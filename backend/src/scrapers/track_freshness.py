"""Freshness of a JRA/netkeiba baba page.

The measurement clock is the one printed next to the reading. A clock is not
invented from ``fetched_at``. ``stale`` is true only when that printed date
and the race date are different days. ``is_today`` compares the same clock
to today in Asia/Tokyo.

Live baba URLs (``/keiba/baba/`` and an undated or same-day ``track.html``)
are not stored in the page cache: the path does not change when JRA replaces
the noon reading. Requests send ``Cache-Control: no-cache``, which asks a
cache to revalidate. That is a normal request header, not a bypass of a
refusal.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import parse_qs, urlsplit

from backend.models.track import TrackInfo
from backend.utils.date_utils import TOKYO, tokyo_now

RETRY_FLOOR_SECONDS = 60.0
DEFAULT_MAX_WAIT_SECONDS = 600.0
STALE_PREFIX = "track measurement is not the race date"
RACE_DATE_UNREADABLE = "track race date was not a date"
CACHED_COPY = "track response was a cached copy"
FRESH_REQUEST_HEADERS = {
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

_STAMP = re.compile(
    r"(?:(\d{4})\s*年\s*)?"
    r"(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"(?:[^0-9\n]{0,16})"
    r"(\d{1,2})\s*[時:：]\s*(\d{1,2})"
)
# Cloudflare says ``HIT``. Akamai says ``TCP_HIT`` / ``TCP_MEM_HIT``. A letter
# glued on either side (``HITS``, ``WHITE``) is not a cache status.
_HIT = re.compile(r"(?<![A-Za-z])HIT(?![A-Za-z])", re.IGNORECASE)
_CACHE_HEADER = ("x-cache", "x-cache-status", "cf-cache-status", "x-akamai-cache")


def polite_interval(requested: float) -> float:
    """At least a minute between refetches. A shorter request is raised."""
    return max(RETRY_FLOOR_SECONDS, float(requested))


def wait_budget_spent(started: float, now: float, interval: float, budget: float) -> bool:
    """True when another polite wait would pass ``budget`` seconds."""
    return now - started + interval > budget


def measurement_times(text: str, year: int | None) -> list[datetime]:
    """Clocks printed as ``M月D日 H時M分``. A 発走 clock is not a measurement.

    The year is the one printed in the same stamp, otherwise ``year``. A stamp
    with no year stays out of the list. Nothing is filled from the fetch time.
    """
    found: list[datetime] = []
    for match in _STAMP.finditer(text):
        tail = text[match.end() : match.end() + 6]
        if "発走" in match.group(0) or "発走" in tail:
            continue
        printed_year = int(match.group(1)) if match.group(1) else year
        if printed_year is None:
            continue
        try:
            found.append(
                datetime(
                    printed_year,
                    int(match.group(2)),
                    int(match.group(3)),
                    int(match.group(4)),
                    int(match.group(5)),
                    tzinfo=TOKYO,
                )
            )
        except ValueError:
            continue
    return found


def page_measurement_time(text: str, year: int | None) -> datetime | None:
    """Latest measurement clock printed on the page. No clock means null."""
    times = measurement_times(text, year)
    if not times:
        return None
    return max(times)


def parse_race_day(value: str | date | datetime | None) -> tuple[date | None, str | None]:
    """``YYYYMMDD`` or ``YYYY-MM-DD``. An empty value is null, not today.

    A string that is not a date returns the unreadable warning and no day.
    """
    if value is None or value == "":
        return None, None
    if isinstance(value, datetime):
        return value.date(), None
    if isinstance(value, date):
        return value, None
    text = str(value).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date(), None
        except ValueError:
            continue
    return None, RACE_DATE_UNREADABLE


def fresh_track_headers(url: str) -> dict[str, str] | None:
    """``Cache-Control: no-cache`` for a live baba or track URL. Other URLs unchanged."""
    if _is_track_url(url):
        return dict(FRESH_REQUEST_HEADERS)
    return None


def should_bypass_page_cache(
    url: str,
    params: dict | None = None,
    *,
    today: date | None = None,
) -> bool:
    """True when this URL is a live track page and must not be replayed from disk.

    JRA baba ``index.html`` is the current meeting, so it is never a historical
    document. A netkeiba track URL for a past ``kaisai_date`` may stay cached.
    """
    if "/keiba/baba/" in url:
        return True
    if "track.html" not in url:
        return False
    raw = _kaisai_date(url, params)
    if raw is None:
        return True
    day, warning = parse_race_day(raw)
    if day is None or warning:
        return True
    current = today or tokyo_now().date()
    return day >= current


def cached_copy_warning(headers: dict | None) -> str | None:
    """Warn when a response header says the body was a cache HIT.

    ``Age`` alone is not a hit: a revalidated response can still carry an age.
    """
    if not headers:
        return None
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    for key in _CACHE_HEADER:
        if _HIT.search(lowered.get(key, "")):
            return CACHED_COPY
    return None


def clear_cached_copy_flag(client: object) -> None:
    """Forget a hit recorded for a previous page on this client."""
    for obj in (
        client,
        getattr(client, "_inner", None),
        getattr(client, "_session", None),
    ):
        if obj is not None and hasattr(obj, "served_cached_copy"):
            obj.served_cached_copy = False


def cached_copy_seen(client: object) -> bool:
    """True when this client, or the HTTP/browser session under it, saw a HIT."""
    for obj in (
        client,
        getattr(client, "_inner", None),
        getattr(client, "_session", None),
    ):
        if getattr(obj, "served_cached_copy", False):
            return True
    return False


def apply_measurement_freshness(
    info: TrackInfo,
    *,
    race_date: date | None = None,
    today: date | None = None,
    race_date_warning: str | None = None,
) -> TrackInfo:
    """Set ``measured_at``, ``is_today``, and ``stale`` from clocks already copied.

    ``race_date`` null means the caller asked for the live meeting, which is
    today in Asia/Tokyo. A warning that the race date could not be read leaves
    ``stale`` null: the comparison day is not invented.
    """
    current = today or tokyo_now().date()
    stamp = _stamp(info)
    warning = _without_stale(info.warning)
    if race_date_warning:
        warning = _join(warning, race_date_warning)
    if stamp is None:
        return info.model_copy(
            update={
                "measured_at": None,
                "is_today": None,
                "stale": None,
                "warning": warning,
            }
        )
    measured_day = stamp.astimezone(TOKYO).date()
    is_today = measured_day == current
    if race_date_warning:
        stale = None
    else:
        compared = race_date or current
        stale = measured_day != compared
        if stale:
            warning = _join(warning, f"{STALE_PREFIX}: {measured_day.isoformat()}")
    return info.model_copy(
        update={
            "measured_at": stamp,
            "is_today": is_today,
            "stale": stale,
            "warning": warning,
        }
    )


def track_blocked(info: TrackInfo) -> bool:
    """403 or a login wall. A later retry must not open the same URL again."""
    warning = info.warning or ""
    return "403" in warning or "requires login" in warning


def needs_fresh_wait(results: dict[str, TrackInfo]) -> bool:
    """True when a reading's printed day is not the race date."""
    return any(info.stale is True and not track_blocked(info) for info in results.values())


def _stamp(info: TrackInfo) -> datetime | None:
    stamps: list[datetime] = []
    if info.measured_at is not None:
        stamps.append(_as_tokyo(info.measured_at))
    for course in (info.turf, info.dirt):
        if course is None:
            continue
        if course.cushion is not None and course.cushion.measured_at is not None:
            stamps.append(_as_tokyo(course.cushion.measured_at))
        if course.moisture is not None and course.moisture.measured_at is not None:
            stamps.append(_as_tokyo(course.moisture.measured_at))
    if not stamps:
        return None
    return max(stamps)


def _as_tokyo(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=TOKYO)
    return value.astimezone(TOKYO)


def _without_stale(warning: str | None) -> str | None:
    if not warning:
        return None
    parts = [part for part in warning.split("; ") if not part.startswith(STALE_PREFIX)]
    if not parts:
        return None
    return "; ".join(parts)


def _join(*parts: str | None) -> str | None:
    items = [part for part in parts if part]
    if not items:
        return None
    return "; ".join(dict.fromkeys(items))


def _is_track_url(url: str) -> bool:
    return "/keiba/baba/" in url or "track.html" in url


def _kaisai_date(url: str, params: dict | None) -> str | None:
    if params:
        raw = params.get("kaisai_date")
        if raw:
            return str(raw)
    query = parse_qs(urlsplit(url).query)
    values = query.get("kaisai_date") or []
    if not values:
        return None
    return values[0]
