"""Resolve netkeiba and JRA race ids from one day's race list.

Kai and day numbers are copied from the list page. They are not guessed from
the calendar date. A 403, captcha, challenge, or login wall stops the list
and is not retried.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from backend.models.race import Race
from backend.scrapers.netkeiba_scraper import NetkeibaScraper
from backend.scrapers.page_fallback import AccessBlocked
from backend.scrapers.race_ids import jra_race_id, list_mismatch_warning, parse_race_id
from backend.utils.date_utils import tokyo_now


class RaceIdRow(BaseModel):
    """One race on a meeting day. Ids that the list did not print are null."""

    netkeiba_race_id: str | None = Field(None, description="開催一覧の12桁。無いときは null")
    jra_race_id: str | None = Field(
        None,
        description="YYYYMMDD + 場コード + レース番号。場かレース番号がページに無いときは null",
    )
    race_date: date = Field(..., description="依頼した開催日")
    venue: str | None = Field(None, description="開催一覧の競馬場")
    venue_code: str | None = Field(None, description="場コード。ページに無いときは null")
    race_number: int | None = Field(None, description="レース番号")
    round_number: int | None = Field(None, description="回。一覧の印字。推測しない")
    day_number: int | None = Field(None, description="日目。一覧の印字。推測しない")
    race_name: str | None = Field(None, description="レース名")
    source: str | None = Field(None, description="netkeiba_html。取れないときは null")
    fetched_at: datetime = Field(..., description="一覧を読んだ時刻。+09:00")
    warning: str | None = Field(None, description="その行を特定できなかった理由")


class RaceIdList(BaseModel):
    """Races for one date, optionally one venue and one race number."""

    race_date: date
    venue: str | None = None
    race_number: int | None = None
    races: list[RaceIdRow] = Field(default_factory=list)
    source: str | None = None
    fetched_at: datetime
    warning: str | None = None


def _block_warning(exc: AccessBlocked) -> str:
    if exc.reason == "captcha":
        return "race list returned a captcha"
    if exc.reason == "challenge":
        return "race list returned a challenge"
    if exc.reason == "http_403" or exc.status == 403:
        return "race list returned 403"
    return "race list requires login"


def _row(race: Race, meeting: date, fetched_at: datetime) -> RaceIdRow:
    parsed = parse_race_id(race.race_id)
    warning = None
    netkeiba_id = race.race_id if parsed.kind == "netkeiba" else None
    if parsed.kind != "netkeiba":
        warning = "netkeiba race id was not on the race list link"
        netkeiba_id = None
    venue_code = race.venue_code or parsed.venue_code
    if race.venue and race.venue != "不明" and parsed.venue and race.venue != parsed.venue:
        warning = (
            "race id does not match the page: "
            f"venue id={parsed.venue} page={race.venue}"
        )
        venue_code = None
    number = race.race_number if race.race_number else None
    return RaceIdRow(
        netkeiba_race_id=netkeiba_id,
        jra_race_id=jra_race_id(meeting, venue_code, number),
        race_date=meeting,
        venue=race.venue if race.venue != "不明" else None,
        venue_code=venue_code,
        race_number=number,
        round_number=race.round_number if race.round_number is not None else parsed.kai,
        day_number=race.day_number if race.day_number is not None else parsed.day_number,
        race_name=race.race_name or None,
        source="netkeiba_html" if netkeiba_id else None,
        fetched_at=fetched_at,
        warning=warning,
    )


class RaceIdService:
    """List race ids for a date from the netkeiba race list."""

    def __init__(self, netkeiba: NetkeibaScraper | None = None):
        self._owns = netkeiba is None
        self.netkeiba = netkeiba or NetkeibaScraper()

    def close(self) -> None:
        if self._owns:
            self.netkeiba.close()

    def __enter__(self) -> RaceIdService:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def resolve(
        self,
        meeting: date,
        venue: str | None = None,
        race_number: int | None = None,
        *,
        fetched_at: datetime | None = None,
    ) -> RaceIdList:
        """Ids for ``meeting``. Venue and race number narrow the list.

        The list is fetched once. A refusal is returned as ``warning`` and an
        empty ``races``. Ids are not guessed, and the list is not requested again.
        """
        fetched = fetched_at or tokyo_now()
        listed: list[Race] = []
        try:
            listed = self.netkeiba.get_races_by_date(meeting)
        except AccessBlocked as exc:
            warning = _block_warning(exc)
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 403:
                warning = "race list returned 403"
            elif status == 401:
                warning = "race list requires login"
            else:
                warning = "race list fetch failed"
        else:
            warning = None
        if warning is not None:
            return RaceIdList(
                race_date=meeting,
                venue=venue,
                race_number=race_number,
                races=[],
                source=None,
                fetched_at=fetched,
                warning=warning,
            )
        rows = [
            _row(race, meeting, fetched)
            for race in listed
            if (venue is None or race.venue == venue)
            and (race_number is None or race.race_number == race_number)
        ]
        warning = None
        if not rows:
            if not listed:
                warning = f"no meeting on {meeting.isoformat()}"
            else:
                warning = "no race matched the venue and race number"
        return RaceIdList(
            race_date=meeting,
            venue=venue,
            race_number=race_number,
            races=rows,
            source="netkeiba_html" if rows else None,
            fetched_at=fetched,
            warning=warning,
        )

    def mismatch(
        self,
        race_id: str,
        meeting: date | None,
        venue: str | None,
    ) -> str | None:
        """Compare ``race_id`` with that day's list. A block is not retried."""
        if meeting is None and not venue:
            parsed = parse_race_id(race_id)
            if parsed.kind is None:
                return "race id was not a netkeiba or JRA id"
            return None
        if meeting is None:
            return "race id check needs a date"
        found = self.resolve(meeting, venue)
        if found.warning and not found.races:
            return found.warning
        ids = {
            row.netkeiba_race_id
            for row in found.races
            if row.netkeiba_race_id
        }
        ids.update(row.jra_race_id for row in found.races if row.jra_race_id)
        return list_mismatch_warning(race_id, meeting=meeting, venue=venue, race_ids=ids)
