"""Load history, then aggregate trends.

A confirmed and complete result page that is already cached is not fetched
again. A result page that is not final is not replayed, so a later run can
pick up a race that settled during the day. A 403 or a login wall stops the
walk. This service does not change the User-Agent, solve a captcha, or send
credentials.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Literal

from backend.models.historical import HISTORICAL_START, HistoricalBatch, HistoricalRace
from backend.models.horse import (
    PEDIGREE_FORBIDDEN,
    PEDIGREE_LOGIN,
    Pedigree,
)
from backend.models.race import Race
from backend.models.race_trends import (
    AS_OF_IGNORED,
    DATE_RANGE_IGNORED,
    DATE_RANGE_REQUIRED,
    HISTORICAL_START_WARNING,
    RECENT_MEETING_LOOKBACK_DAYS,
    RECENT_MEETING_NEEDS_VENUE,
    SIRE_NOT_LOADED,
    TODAY_CONFLICT,
    ExcludedRace,
    FlatSurface,
    Going,
    RaceTrendReport,
    RecentMeetingWindow,
    TrendFilter,
)
from backend.scrapers.page_fallback import AccessBlocked
from backend.scrapers.runner_status import join_warnings
from backend.services.historical_service import HistoricalService
from backend.services.race_trend_aggregate import aggregate_race_trends
from backend.utils.date_utils import tokyo_now

logger = logging.getLogger(__name__)

ListRaces = Callable[[date], Sequence[Race]]
PedigreeLookup = Callable[[str], Pedigree]
_PEDIGREE_BLOCKS = frozenset({PEDIGREE_FORBIDDEN, PEDIGREE_LOGIN})


class RaceTrendService:
    """Trends over the historical race records and the local page cache."""

    def __init__(
        self,
        *,
        historical: HistoricalService | None = None,
        list_races: ListRaces | None = None,
        pedigree_lookup: PedigreeLookup | None = None,
        clock: Callable[[], date] | None = None,
        fetch_mode: Literal["html", "headless", "auto"] = "html",
    ):
        self._owns = historical is None
        self._historical = (
            historical if historical is not None else HistoricalService(fetch_mode=fetch_mode)
        )
        self._list_races = list_races
        self._pedigree_lookup = pedigree_lookup
        self._today = clock or _tokyo_today

    def _list(self, day: date) -> Sequence[Race]:
        if self._list_races is not None:
            return self._list_races(day)
        return self._historical.netkeiba.get_races_by_date(day)

    def _lookup_pedigree(self, horse_id: str) -> Pedigree:
        if self._pedigree_lookup is not None:
            return self._pedigree_lookup(horse_id)
        return self._historical.netkeiba.get_pedigree(horse_id)

    def close(self) -> None:
        if self._owns:
            self._historical.close()

    def __enter__(self) -> RaceTrendService:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> Literal[False]:
        self.close()
        return False

    def get(
        self,
        *,
        venues: Sequence[str] | None = None,
        surface: str | None = None,
        distance: int | None = None,
        race_class: str | None = None,
        going: str | None = None,
        on_date: date | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        recent_meeting: bool = False,
        as_of: date | None = None,
        today: bool = False,
        sires: Mapping[str, Pedigree] | None = None,
    ) -> RaceTrendReport:
        """Filter settled races and return the trend tables.

        ``today`` is one meeting day, today in Asia/Tokyo. It does not take
        another date. ``sires`` skips the pedigree fetch. Tests pass it.
        Production leaves it empty so each horse id is read once from the
        existing pedigree code.
        """
        computed_at = tokyo_now()
        selected: list[str] = []
        for venue in venues or []:
            text = _text(venue)
            if text is not None:
                selected.append(text)
        if today and (on_date or date_from or date_to or recent_meeting or as_of):
            return _empty(
                TrendFilter(venues=selected, today=True),
                computed_at,
                TODAY_CONFLICT,
            )
        if today:
            on_date = self._today()
        filt = TrendFilter(
            venues=selected,
            surface=_surface(surface),
            distance=distance,
            race_class=_text(race_class),
            going=_going(going),
            date_from=on_date or date_from,
            date_to=on_date or date_to or date_from,
            recent_meeting=recent_meeting,
            as_of=as_of,
            today=today,
        )
        invalid = _invalid_filter(surface, going)
        if invalid:
            return _empty(filt, computed_at, invalid)
        if recent_meeting:
            return self._recent(filt, computed_at, sires, date_from or date_to or on_date)
        if filt.date_from is None:
            return _empty(
                filt, computed_at, DATE_RANGE_REQUIRED, as_of_note=AS_OF_IGNORED if as_of else None
            )
        note = AS_OF_IGNORED if as_of else None
        batch = self._historical.get(
            on_date=on_date,
            date_from=None if on_date else date_from,
            date_to=None if on_date else date_to,
            venues=selected,
            flat_only=True,
        )
        return self._from_batch(filt, batch, computed_at, sires, extra_note=note)

    def _recent(
        self,
        filt: TrendFilter,
        computed_at: datetime,
        sires: Mapping[str, Pedigree] | None,
        had_dates: date | None,
    ) -> RaceTrendReport:
        as_of = filt.as_of or self._today()
        filt = filt.model_copy(update={"as_of": as_of, "date_from": None, "date_to": None})
        notes = [DATE_RANGE_IGNORED if had_dates else None]
        if len(filt.venues) != 1:
            window = RecentMeetingWindow(venue=None, as_of=as_of, previous_meeting_date=None)
            window = window.model_copy(update={"warning": RECENT_MEETING_NEEDS_VENUE})
            return _empty(
                filt,
                computed_at,
                join_warnings(RECENT_MEETING_NEEDS_VENUE, *notes),
                window=window,
            )
        if as_of < HISTORICAL_START:
            window = RecentMeetingWindow(
                venue=filt.venues[0],
                as_of=as_of,
                previous_meeting_date=None,
                warning=HISTORICAL_START_WARNING,
            )
            return _empty(
                filt, computed_at, join_warnings(HISTORICAL_START_WARNING, *notes), window=window
            )
        venue = filt.venues[0]
        previous, look_warning, blocked = previous_meeting_date(
            self._list, venue=venue, as_of=as_of
        )
        window = RecentMeetingWindow(
            venue=venue,
            as_of=as_of,
            previous_meeting_date=previous,
            warning=look_warning,
        )
        if blocked:
            return _empty(
                filt,
                computed_at,
                join_warnings(look_warning, *notes),
                window=window,
            )
        batches: list[HistoricalBatch] = []
        if previous is not None:
            previous_batch = self._historical.get(on_date=previous, venues=[venue], flat_only=True)
            batches.append(previous_batch)
            if previous_batch.stopped_at_race_id:
                return self._from_batches(
                    filt, batches, computed_at, sires, window, join_warnings(*notes)
                )
        today_batch = self._historical.get(on_date=as_of, venues=[venue], flat_only=True)
        batches.append(today_batch)
        return self._from_batches(filt, batches, computed_at, sires, window, join_warnings(*notes))

    def _from_batch(
        self,
        filt: TrendFilter,
        batch: HistoricalBatch,
        computed_at: datetime,
        sires: Mapping[str, Pedigree] | None,
        extra_note: str | None = None,
        window: RecentMeetingWindow | None = None,
    ) -> RaceTrendReport:
        return self._from_batches(filt, [batch], computed_at, sires, window, extra_note)

    def _from_batches(
        self,
        filt: TrendFilter,
        batches: Sequence[HistoricalBatch],
        computed_at: datetime,
        sires: Mapping[str, Pedigree] | None,
        window: RecentMeetingWindow | None,
        extra_note: str | None,
    ) -> RaceTrendReport:
        races: list[HistoricalRace] = []
        seen: set[str] = set()
        extra: list[ExcludedRace] = []
        notes: list[str | None] = [extra_note]
        skipped = 0
        for batch in batches:
            notes.append(batch.warning)
            skipped += batch.skipped
            for race in batch.races:
                if race.race_id in seen:
                    continue
                seen.add(race.race_id)
                races.append(race)
            extra.extend(_remaining(batch, seen))
            for item in extra:
                seen.add(item.race_id)
        report = aggregate_race_trends(
            races,
            filt=filt,
            sires={} if sires is None else sires,
            sire_unfetched={},
            extra_excluded=extra,
            recent_meeting=window,
            batch_warning=join_warnings(*notes),
            history_skipped=skipped,
            computed_at=computed_at,
        )
        if sires is not None:
            return aggregate_race_trends(
                races,
                filt=filt,
                sires=sires,
                sire_unfetched={},
                extra_excluded=extra,
                recent_meeting=window,
                batch_warning=join_warnings(*notes),
                history_skipped=skipped,
                computed_at=computed_at,
            )
        loaded, unfetched, stop = _load_sires(self._lookup_pedigree, _horse_ids(report, races))
        stop_note = None if stop is None else f"{SIRE_NOT_LOADED}: {stop}"
        return aggregate_race_trends(
            races,
            filt=filt,
            sires=loaded,
            sire_unfetched=unfetched,
            extra_excluded=extra,
            recent_meeting=window,
            batch_warning=join_warnings(*notes, stop_note),
            history_skipped=skipped,
            computed_at=computed_at,
        )


def previous_meeting_date(
    list_races: ListRaces,
    *,
    venue: str,
    as_of: date,
    lookback_days: int = RECENT_MEETING_LOOKBACK_DAYS,
) -> tuple[date | None, str | None, bool]:
    """The latest day before ``as_of`` with a listed race at ``venue``.

    A 403 or a login wall returns ``blocked`` true and does not open an earlier day.
    A failed list does not skip that day. An empty list is not a meeting.
    """
    earliest = as_of - timedelta(days=lookback_days)
    if earliest < HISTORICAL_START:
        earliest = HISTORICAL_START
    day = as_of - timedelta(days=1)
    while day >= earliest:
        try:
            listed = list(list_races(day))
        except AccessBlocked as exc:
            logger.warning("Race list blocked for %s: %s", day, exc)
            if exc.status == 403:
                return None, "race list returned 403", True
            return None, "race list requires login", True
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.warning("Race list failed for %s: %s", day, exc)
            if status == 403:
                return None, "race list returned 403", True
            if status == 401:
                return None, "race list requires login", True
            return None, f"race list failed for {day.isoformat()}", False
        if any(_text(race.venue) == venue for race in listed):
            return day, None, False
        day -= timedelta(days=1)
    return None, f"previous meeting day was not found within {lookback_days} days", False


def _load_sires(
    lookup: PedigreeLookup,
    horse_ids: Sequence[str],
) -> tuple[dict[str, Pedigree], dict[str, str], str | None]:
    """One pedigree read per horse. A 403 or login stops the rest of the ids."""
    loaded: dict[str, Pedigree] = {}
    unfetched: dict[str, str] = {}
    stop: str | None = None
    for horse_id in horse_ids:
        if stop is not None:
            unfetched[horse_id] = f"{SIRE_NOT_LOADED}: {stop}"
            continue
        pedigree = lookup(horse_id)
        loaded[horse_id] = pedigree
        blocked = _pedigree_block(pedigree.warning)
        if blocked:
            stop = blocked
    return loaded, unfetched, stop


def _horse_ids(report: RaceTrendReport, races: Sequence[HistoricalRace]) -> list[str]:
    included = set(report.included_race_ids)
    found: list[str] = []
    seen: set[str] = set()
    for race in races:
        if race.race_id not in included:
            continue
        for finisher in race.result.results:
            horse_id = _text(finisher.horse_id)
            if horse_id and horse_id not in seen:
                seen.add(horse_id)
                found.append(horse_id)
    return found


def _remaining(batch: HistoricalBatch, seen: set[str]) -> list[ExcludedRace]:
    reason = batch.warning or "not fetched"
    rows: list[ExcludedRace] = []
    for race_id in batch.remaining_race_ids:
        if race_id in seen:
            continue
        if race_id == batch.stopped_at_race_id:
            rows.append(ExcludedRace(race_id=race_id, reason=reason))
        else:
            rows.append(ExcludedRace(race_id=race_id, reason=f"not fetched: {reason}"))
    return rows


def _pedigree_block(warning: str | None) -> str | None:
    if not warning:
        return None
    for piece in warning.split("; "):
        if piece in _PEDIGREE_BLOCKS:
            return piece
    return None


def _empty(
    filt: TrendFilter,
    computed_at: datetime,
    warning: str | None,
    *,
    window: RecentMeetingWindow | None = None,
    as_of_note: str | None = None,
) -> RaceTrendReport:
    return aggregate_race_trends(
        [],
        filt=filt,
        recent_meeting=window,
        batch_warning=join_warnings(warning, as_of_note),
        computed_at=computed_at,
    )


def _surface(value: str | None) -> FlatSurface | None:
    text = _text(value)
    if text in {"芝", "ダート"}:
        return text  # type: ignore[return-value]
    return None


def _invalid_filter(surface: str | None, going: str | None) -> str | None:
    if _text(surface) and _surface(surface) is None:
        return "surface filter must be 芝 or ダート"
    if _text(going) and _going(going) is None:
        return "going filter must be 良, 稍重, 重, or 不良"
    return None


def _going(value: str | None) -> Going | None:
    text = _text(value)
    if text in {"良", "稍重", "重", "不良"}:
        return text  # type: ignore[return-value]
    return None


def _tokyo_today() -> date:
    """The meeting day. The host clock is not used."""
    return tokyo_now().date()


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
