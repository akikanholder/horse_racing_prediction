"""Collect past JRA flat races for an EV backtest.

One race, or a date span fetched one race at a time. JRA is preferred for
final odds, then a netkeiba post-race payload, then win odds printed on the
result page. Live odds are never stored as final.
"""

from __future__ import annotations

import csv
import json
import logging
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from backend.models.historical import (
    HISTORICAL_REQUEST_DELAY,
    HISTORICAL_START,
    PRIORITY_VENUES,
    HistoricalBatch,
    HistoricalExport,
    HistoricalRace,
)
from backend.models.odds import ODDS_ENCODING, BetType, NonRunner, OddsQuote, RaceOdds
from backend.models.race import Race, SettledRaceResult
from backend.scrapers.final_odds import (
    FINAL_ODDS_NOT_CONFIRMED,
    FinalOddsParse,
    missing_market_warning,
    non_runners_from_finishers,
    parse_confirmed_odds,
    quotes_from_settled_win,
    result_is_post_race,
)
from backend.scrapers.jra_scraper import JRAScraper
from backend.scrapers.netkeiba_odds_scraper import NetkeibaOddsScraper
from backend.scrapers.netkeiba_scraper import NetkeibaScraper
from backend.scrapers.page_cache import CachingHTTPClient, default_page_cache_dir
from backend.scrapers.runner_status import flag_quotes, join_warnings, merge_non_runners

logger = logging.getLogger(__name__)

RESULT_URL = "https://race.netkeiba.com/race/result.html"
_FLAT_SURFACES = frozenset({"芝", "ダート"})
_POST_BET_TYPES: tuple[BetType, ...] = ("win", "place", "wide", "umaren", "sanrenpuku")
_RUNNER_FIELDS = [
    "race_id",
    "race_date",
    "venue",
    "surface",
    "distance",
    "race_class",
    "field_size",
    "going",
    "horse_number",
    "horse_name",
    "rank",
    "status",
    "status_label",
    "final_win_odds",
    "popularity",
    "win_payout",
    "wide_payout",
    "umaren_payout",
    "sanrenpuku_payout",
    "odds_source",
    "odds_fetched_at",
    "result_source",
    "fetched_at",
]
_PAYOUT_NAMES = {
    "win": "単勝",
    "wide": "ワイド",
    "umaren": "馬連",
    "sanrenpuku": "3連複",
}


class HistoricalService:
    """Read historical races through the public JRA then netkeiba pages."""

    def __init__(
        self,
        *,
        netkeiba: NetkeibaScraper | None = None,
        jra: JRAScraper | None = None,
        odds: NetkeibaOddsScraper | None = None,
        cache_dir: Path | None = None,
        delay_seconds: float = HISTORICAL_REQUEST_DELAY,
        sleeper: Callable[[float], None] = time.sleep,
        headless_render: Literal["auto", "off"] = "auto",
    ):
        self.delay_seconds = delay_seconds
        self._sleeper = sleeper
        self._owns_clients = netkeiba is None and jra is None and odds is None
        self.http: CachingHTTPClient | None = None
        if self._owns_clients:
            self.http = CachingHTTPClient(
                cache_dir or default_page_cache_dir(),
                min_request_interval=delay_seconds,
            )
            self.netkeiba = NetkeibaScraper(self.http, headless_render=headless_render)
            self.jra = JRAScraper(self.http, headless_render=headless_render)
            self.odds = NetkeibaOddsScraper(self.http)
        else:
            if netkeiba is None or jra is None or odds is None:
                raise ValueError("netkeiba, jra, and odds are passed together")
            self.netkeiba = netkeiba
            self.jra = jra
            self.odds = odds

    def close(self) -> None:
        """Close clients this service constructed."""
        if not self._owns_clients:
            return
        self.netkeiba.close()
        self.jra.close()
        self.odds.close()
        if self.http is not None:
            self.http.close()

    def __enter__(self) -> HistoricalService:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> Literal[False]:
        self.close()
        return False

    def get(
        self,
        *,
        race_id: str | None = None,
        on_date: date | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        venues: Sequence[str] | None = None,
        flat_only: bool = True,
        priority_venues: bool = False,
    ) -> HistoricalBatch:
        """One race, one day, or an inclusive date range.

        Dates before 2025 are not requested. Races are fetched one after another
        with ``delay_seconds`` between them. A direct ``race_id`` is returned
        even when it is out of scope, with ``in_scope`` false.
        """
        fetched_at = datetime.now()
        selected = _venue_filter(venues, priority_venues)
        if race_id:
            record = self._one(race_id, fetched_at)
            record = _apply_scope(record, venues=selected, flat_only=flat_only)
            warning = None if record.in_scope else record.warning
            return HistoricalBatch(
                races=[record],
                skipped=0,
                flat_only=flat_only,
                venues=list(selected),
                date_from=None,
                date_to=None,
                fetched_at=fetched_at,
                warning=warning,
            )

        start, end, range_warning = _resolve_span(on_date, date_from, date_to)
        if start is None or end is None:
            return HistoricalBatch(
                races=[],
                skipped=0,
                flat_only=flat_only,
                venues=list(selected),
                date_from=None,
                date_to=None,
                fetched_at=fetched_at,
                warning=range_warning or "historical races start in 2025",
            )
        races: list[HistoricalRace] = []
        skipped = 0
        notes: list[str | None] = [range_warning]
        first = True
        day = start
        while day <= end:
            try:
                listed = self.netkeiba.get_races_by_date(day)
            except Exception as exc:
                logger.warning("Race list failed for %s: %s", day, exc)
                notes.append(f"race list failed for {day.isoformat()}")
                listed = []
            for race in listed:
                if not _list_row_in_play(race, selected, flat_only):
                    skipped += 1
                    continue
                if not first and self.delay_seconds > 0:
                    self._sleeper(self.delay_seconds)
                first = False
                record = self._one(race.race_id, datetime.now())
                record = _apply_scope(record, venues=selected, flat_only=flat_only)
                if record.in_scope:
                    races.append(record)
                else:
                    skipped += 1
            day += timedelta(days=1)
        if skipped:
            notes.append(f"excluded {skipped} races outside the flat 2025 venue filter")
        return HistoricalBatch(
            races=races,
            skipped=skipped,
            flat_only=flat_only,
            venues=list(selected),
            date_from=start,
            date_to=end,
            fetched_at=fetched_at,
            warning=join_warnings(*notes),
        )

    @staticmethod
    def export(
        batch: HistoricalBatch,
        directory: Path,
        fmt: str,
    ) -> HistoricalExport:
        """Write in-scope races as JSONL or as CSV split by surface."""
        directory.mkdir(parents=True, exist_ok=True)
        chosen = [race for race in batch.races if race.in_scope]
        present = {race.surface for race in chosen if race.surface}
        surfaces: list[str] = []
        for surface in ("芝", "ダート"):
            if surface in present:
                surfaces.append(surface)
        if fmt == "jsonl":
            path = directory / "historical.jsonl"
            with path.open("w", encoding="utf-8") as handle:
                for race in chosen:
                    handle.write(json.dumps(race.model_dump(mode="json"), ensure_ascii=False))
                    handle.write("\n")
            return HistoricalExport(
                format="jsonl",
                directory=str(directory),
                paths=[str(path)],
                race_count=len(chosen),
                row_count=len(chosen),
                surfaces=surfaces,
            )
        rows = [_runner_row(race) for race in chosen]
        flat_rows = [row for group in rows for row in group]
        paths = [_write_csv(directory / "runners.csv", flat_rows)]
        for surface in surfaces:
            surface_rows = [row for row in flat_rows if row["surface"] == surface]
            paths.append(_write_csv(directory / f"runners_{surface}.csv", surface_rows))
        return HistoricalExport(
            format="csv",
            directory=str(directory),
            paths=paths,
            race_count=len(chosen),
            row_count=len(flat_rows),
            surfaces=surfaces,
        )

    def _one(self, race_id: str, fetched_at: datetime) -> HistoricalRace:
        stamped = self._cached_stamp(race_id) or fetched_at
        settled = self.netkeiba.get_settled_race_result(race_id, fetched_at=stamped)
        final_quotes, odds_runners, odds_source, odds_warning, odds_final = self._final_odds(
            race_id, settled
        )
        runners = merge_non_runners(
            [
                None
                if settled.warning
                in {
                    "race result fetch failed",
                    "race result not found",
                }
                and not settled.results
                else non_runners_from_finishers(settled.results),
                odds_runners,
            ]
        )
        if runners is None and settled.results:
            runners = []
        if final_quotes is not None and runners is not None:
            final_quotes = flag_quotes(final_quotes, runners, set(), prefer_runners=True)
        surface = settled.track_type if settled.track_type in _FLAT_SURFACES else None
        flat: bool | None
        if settled.track_type == "障害":
            flat = False
        elif surface is not None:
            flat = True
        else:
            flat = None
        field_size = settled.entry_count
        if field_size is None and settled.results:
            field_size = len(settled.results)
        warning = join_warnings(
            settled.warning,
            odds_warning,
            None if settled.race_class else "race class was not on the page",
            None if field_size is not None else "field size was not on the page",
            None if settled.track_condition else "going was not on the page",
            None if settled.track_type else "surface was not on the page",
        )
        return HistoricalRace(
            race_id=race_id,
            surface=surface,  # type: ignore[arg-type]
            distance=settled.distance,
            race_class=settled.race_class,
            field_size=field_size,
            going=settled.track_condition,
            flat=flat,
            in_scope=False,
            result=settled,
            final_odds=final_quotes,
            non_runners=runners,
            odds_final=odds_final and final_quotes is not None,
            source=_join_source(settled.source, odds_source),
            fetched_at=stamped,
            warning=warning,
        )

    def _final_odds(
        self,
        race_id: str,
        settled: SettledRaceResult,
    ) -> tuple[list[OddsQuote] | None, list[NonRunner] | None, str | None, str | None, bool]:
        pages = self.jra.fetch_odds_pages(race_id, fetched_at=settled.fetched_at)
        parsed = parse_confirmed_odds(pages.pages, race_id)
        notes: list[str | None] = [pages.warning, parsed.warning]
        quotes = list(parsed.quotes) if parsed.confirmed else []
        source = parsed.source
        if not _has_win(quotes):
            post_quotes, post_runners, post_source, post_note = self._post_race_api(race_id)
            notes.append(post_note)
            if post_quotes:
                quotes = _fill_missing(quotes, post_quotes)
                source = _join_source(source, post_source)
                if parsed.non_runners:
                    parsed = FinalOddsParse(
                        True,
                        quotes,
                        _merge_runner_lists(parsed.non_runners, post_runners),
                        source,
                        parsed.warning,
                    )
                else:
                    parsed = FinalOddsParse(True, quotes, post_runners, source, parsed.warning)
        if not _has_win(quotes) and result_is_post_race(settled):
            quotes = _fill_missing(quotes, quotes_from_settled_win(settled))
            source = _join_source(source, settled.source)
        if not quotes:
            if result_is_post_race(settled):
                notes.append("final win odds were not on the page")
            else:
                notes.append(FINAL_ODDS_NOT_CONFIRMED)
            return None, parsed.non_runners or None, source, join_warnings(*notes), False
        notes = [note for note in notes if note != FINAL_ODDS_NOT_CONFIRMED]
        notes.append(missing_market_warning(quotes))
        runners = parsed.non_runners
        return quotes, runners, source, join_warnings(*notes), True

    def _post_race_api(
        self,
        race_id: str,
    ) -> tuple[list[OddsQuote], list[NonRunner], str | None, str | None]:
        """Netkeiba odds API only when status is result (race_phase post).

        A pre-race payload is discarded. Its numbers are not returned.
        """
        win = self._fetch_bet(race_id, "win")
        if win.race_phase != "post":
            if win.quotes or win.race_phase == "pre":
                return [], [], None, FINAL_ODDS_NOT_CONFIRMED
            return [], [], None, win.reason
        quotes = list(win.quotes)
        runners: list[NonRunner] = list(win.non_runners or [])
        for bet_type in _POST_BET_TYPES:
            if bet_type == "win":
                continue
            snapshot = self._fetch_bet(race_id, bet_type)
            if snapshot.race_phase != "post":
                continue
            quotes.extend(snapshot.quotes)
            runners.extend(snapshot.non_runners or [])
        if not quotes:
            return [], runners, None, win.reason or "final win odds were not on the page"
        return quotes, runners, "netkeiba_live", None

    def _fetch_bet(self, race_id: str, bet_type: BetType) -> RaceOdds:
        fetched_at = datetime.now()
        fetch = {
            "win": self.odds.get_win_odds,
            "place": self.odds.get_place_odds,
            "wide": self.odds.get_wide_odds,
            "umaren": self.odds.get_umaren_odds,
            "sanrenpuku": self.odds.get_sanrenpuku_odds,
        }[bet_type]
        try:
            return fetch(race_id, fetched_at=fetched_at)
        except Exception as exc:
            logger.warning("Final %s odds failed for %s: %s", bet_type, race_id, exc)
            return RaceOdds(
                race_id=race_id,
                bet_types=[bet_type],
                prefer="live",
                status="unavailable",
                reason="fetch failed",
                encoding=ODDS_ENCODING,
                race_phase=None,
                quotes=[],
                non_runners=None,
                warning=None,
                fetched_at=fetched_at,
            )

    def _cached_stamp(self, race_id: str) -> datetime | None:
        if self.http is None:
            return None
        return self.http.cached_fetched_at("GET", RESULT_URL, {"race_id": race_id})


def _venue_filter(venues: Sequence[str] | None, priority: bool) -> list[str]:
    if priority and not venues:
        return list(PRIORITY_VENUES)
    if priority and venues:
        allowed = set(PRIORITY_VENUES)
        return [venue for venue in venues if venue in allowed]
    return [venue for venue in venues or [] if venue]


def _resolve_span(
    on_date: date | None,
    date_from: date | None,
    date_to: date | None,
) -> tuple[date | None, date | None, str | None]:
    start: date | None
    end: date | None
    if on_date is not None:
        start = end = on_date
    else:
        start = date_from
        end = date_to or date_from
    if start is None or end is None:
        return None, None, "a date or date range is required"
    if end < start:
        return None, None, "date range ends before it starts"
    note = None
    if end < HISTORICAL_START:
        return None, None, "historical races start in 2025"
    if start < HISTORICAL_START:
        start = HISTORICAL_START
        note = "historical races start in 2025"
    return start, end, note


def _list_row_in_play(race: Race, venues: list[str], flat_only: bool) -> bool:
    if venues and race.venue not in venues:
        return False
    if flat_only and race.track_type == "障害":
        return False
    return True


def _apply_scope(
    race: HistoricalRace,
    *,
    venues: list[str],
    flat_only: bool,
) -> HistoricalRace:
    notes: list[str | None] = []
    in_scope = True
    race_date = race.result.race_date
    if race_date is None:
        in_scope = False
        notes.append("race date was not on the page")
    elif race_date < HISTORICAL_START:
        in_scope = False
        notes.append("historical races start in 2025")
    if flat_only and race.flat is False:
        in_scope = False
        notes.append("jump races are excluded")
    elif flat_only and race.flat is None:
        in_scope = False
        notes.append("surface was not on the page")
    venue = race.result.venue
    if venues and venue not in venues:
        in_scope = False
        notes.append("venue is outside the filter")
    warning = join_warnings(race.warning, *notes)
    if warning == race.warning and in_scope == race.in_scope:
        return race
    return race.model_copy(update={"in_scope": in_scope, "warning": warning})


def _has_win(quotes: list[OddsQuote]) -> bool:
    return any(quote.bet_type == "win" for quote in quotes)


def _fill_missing(primary: list[OddsQuote], extra: list[OddsQuote]) -> list[OddsQuote]:
    seen = {(quote.bet_type, quote.combination) for quote in primary}
    merged = list(primary)
    for quote in extra:
        key = (quote.bet_type, quote.combination)
        if key in seen:
            continue
        seen.add(key)
        merged.append(quote)
    return merged


def _merge_runner_lists(primary: list[NonRunner], extra: list[NonRunner]) -> list[NonRunner]:
    merged = merge_non_runners([primary, extra])
    return merged or []


def _join_source(*parts: str | None) -> str | None:
    found: list[str] = []
    for part in parts:
        if not part:
            continue
        for piece in part.split("+"):
            if piece and piece not in found:
                found.append(piece)
    if not found:
        return None
    return "+".join(found)


def _runner_row(race: HistoricalRace) -> list[dict[str, str]]:
    win_quotes = {
        quote.combination: quote for quote in race.final_odds or [] if quote.bet_type == "win"
    }
    payouts = race.result.payouts
    rows: list[dict[str, str]] = []
    for finisher in race.result.results:
        number = "" if finisher.horse_number is None else str(finisher.horse_number)
        quote = win_quotes.get(number)
        odds = ""
        odds_source = ""
        odds_fetched = ""
        if quote is not None and quote.odds is not None:
            odds = _num(quote.odds)
            odds_source = quote.source
            odds_fetched = quote.fetched_at.isoformat(timespec="seconds")
        rows.append(
            {
                "race_id": race.race_id,
                "race_date": race.result.race_date.isoformat() if race.result.race_date else "",
                "venue": race.result.venue or "",
                "surface": race.surface or "",
                "distance": "" if race.distance is None else str(race.distance),
                "race_class": race.race_class or "",
                "field_size": "" if race.field_size is None else str(race.field_size),
                "going": race.going or "",
                "horse_number": number,
                "horse_name": finisher.horse_name or "",
                "rank": "" if finisher.rank is None else str(finisher.rank),
                "status": finisher.status or "",
                "status_label": finisher.status_label or "",
                "final_win_odds": odds,
                "popularity": "" if finisher.popularity is None else str(finisher.popularity),
                "win_payout": _payout_text(payouts, "win"),
                "wide_payout": _payout_text(payouts, "wide"),
                "umaren_payout": _payout_text(payouts, "umaren"),
                "sanrenpuku_payout": _payout_text(payouts, "sanrenpuku"),
                "odds_source": odds_source,
                "odds_fetched_at": odds_fetched,
                "result_source": finisher.source or "",
                "fetched_at": race.fetched_at.isoformat(timespec="seconds"),
            }
        )
    return rows


def _payout_text(payouts: list, bet: str) -> str:
    label = _PAYOUT_NAMES[bet]
    parts = [f"{row.combination}:{row.payout}" for row in payouts if row.bet_type == label]
    return ";".join(parts)


def _write_csv(path: Path, rows: list[dict[str, str]]) -> str:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_RUNNER_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


def _num(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return str(value)
