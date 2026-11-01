"""netkeiba and JRA race ids.

A netkeiba id is year + venue + kai + day + race. The kai and the day come
from that day's race list, not from the calendar date. A project id is
``YYYYMMDD`` + venue code + race number. Digits are not turned into a date
when the id is the netkeiba form.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

VENUE_CODES: dict[str, str] = {
    "01": "札幌",
    "02": "函館",
    "03": "福島",
    "04": "新潟",
    "05": "東京",
    "06": "中山",
    "07": "中京",
    "08": "京都",
    "09": "阪神",
    "10": "小倉",
}
VENUE_BY_NAME = {name: code for code, name in VENUE_CODES.items()}


@dataclass(frozen=True)
class ParsedRaceId:
    """Pieces printed in an id. A missing piece is null."""

    kind: str | None
    year: int | None
    venue_code: str | None
    venue: str | None
    kai: int | None
    day_number: int | None
    race_number: int | None
    race_date: date | None


def parse_race_id(race_id: str) -> ParsedRaceId:
    """Read a 12-digit netkeiba id, or a project id. Anything else is empty."""
    empty = ParsedRaceId(None, None, None, None, None, None, None, None)
    if len(race_id) < 12 or not race_id[:12].isdigit():
        return empty
    body = race_id[:12]
    year = int(body[:4])
    race_number = int(body[10:12])
    netkeiba_code = body[4:6]
    kai = int(body[6:8])
    day_number = int(body[8:10])
    if (
        netkeiba_code in VENUE_CODES
        and 1 <= kai <= 6
        and 1 <= day_number <= 12
        and 1 <= race_number <= 12
    ):
        return ParsedRaceId(
            kind="netkeiba",
            year=year,
            venue_code=netkeiba_code,
            venue=VENUE_CODES[netkeiba_code],
            kai=kai,
            day_number=day_number,
            race_number=race_number,
            race_date=None,
        )
    try:
        meeting = date(int(body[:4]), int(body[4:6]), int(body[6:8]))
    except ValueError:
        return empty
    project_code = body[8:10]
    if project_code not in VENUE_CODES or not 1 <= race_number <= 12:
        return empty
    return ParsedRaceId(
        kind="project",
        year=meeting.year,
        venue_code=project_code,
        venue=VENUE_CODES[project_code],
        kai=None,
        day_number=None,
        race_number=race_number,
        race_date=meeting,
    )


def jra_race_id(meeting: date, venue_code: str | None, race_number: int | None) -> str | None:
    """Project id from a list row. A missing venue code or race number stays null."""
    if venue_code not in VENUE_CODES or race_number is None or not 1 <= race_number <= 12:
        return None
    return f"{meeting.strftime('%Y%m%d')}{venue_code}{race_number:02d}"


def page_mismatch_warning(
    race_id: str,
    *,
    venue: str | None = None,
    round_number: int | None = None,
    day_number: int | None = None,
    race_number: int | None = None,
    race_date: date | None = None,
) -> str | None:
    """Warn when the page's meeting does not match the id. Do not rewrite the id.

    A netkeiba id has no calendar date, so ``race_date`` is compared only for
    a project id. ``不明`` is not a venue.
    """
    parsed = parse_race_id(race_id)
    if parsed.kind is None:
        return "race id was not a netkeiba or JRA id"
    parts: list[str] = []
    if venue and venue != "不明" and parsed.venue and venue != parsed.venue:
        parts.append(f"venue id={parsed.venue} page={venue}")
    if race_number is not None and parsed.race_number is not None and race_number != parsed.race_number:
        parts.append(f"race id={parsed.race_number} page={race_number}")
    if parsed.kind == "netkeiba":
        if round_number is not None and parsed.kai is not None and round_number != parsed.kai:
            parts.append(f"kai id={parsed.kai} page={round_number}")
        if day_number is not None and parsed.day_number is not None and day_number != parsed.day_number:
            parts.append(f"day id={parsed.day_number} page={day_number}")
    elif race_date is not None and parsed.race_date is not None and race_date != parsed.race_date:
        parts.append(f"date id={parsed.race_date.isoformat()} page={race_date.isoformat()}")
    if not parts:
        return None
    return "race id does not match the page: " + "; ".join(parts)


def list_mismatch_warning(
    race_id: str,
    *,
    meeting: date,
    venue: str | None,
    race_ids: set[str],
) -> str | None:
    """Warn when ``race_id`` is not on that day's list for the venue."""
    if race_id in race_ids:
        return None
    place = venue or "the meeting"
    return f"race id does not match {meeting.isoformat()} {place}"
