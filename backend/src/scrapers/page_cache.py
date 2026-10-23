"""Local cache and a pause between public-page requests.

Date ranges call sites one request at a time. A cached final page is not
fetched again. A ``result.html`` that is not confirmed and complete is not
stored and is not replayed, including a copy left from an earlier run.
A race list for today or a later day in Asia/Tokyo is not stored either.
JRA payout (accessH), odds (accessO), and race-card (accessD) pages for
today or later, and undated menus on those endpoints, are not stored.
The netkeiba odds API is stored only when its status is ``result`` and the
odds map is non-empty. An ``official_datetime`` of today or later in
Asia/Tokyo is not stored. ``odds/index.html`` is stored only when a price
was extracted. A shell of ``---.-`` is not stored and is not replayed, even
when an older file was marked replayable. A caller that already knows the
race day is today can set ``replay_disabled`` so the odds pages are not
stored either. HTTP 403, a login wall, and a challenge page are not stored.
A legacy file with no replayable mark is checked again on read, so an old
denial is not replayed. A file this client marked replayable was checked at
write time and is not parsed again for that denial. ``result.html``, the
odds API, and ``odds/index.html`` are still judged from the body. This
client does not change the User-Agent or send credentials.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from backend.scrapers.final_odds import _has_price_text, odds_api_is_replayable
from backend.scrapers.netkeiba_scraper import result_html_is_final, result_html_is_partial
from backend.scrapers.page_fallback import looks_like_forbidden_page, looks_like_login_wall
from backend.scrapers.track_freshness import parse_race_day, should_bypass_page_cache
from backend.utils.date_utils import tokyo_now
from backend.utils.http_client import HTTPClient

logger = logging.getLogger(__name__)

_RACE_LIST_URLS = (
    "/top/race_list.html",
    "race_list_get_date_list.html",
    "race_list_sub.html",
)
_JRA_LIVE_URLS = ("accessH.html", "accessO.html", "accessD.html")
_ODDS_API = "api_get_jra_odds.html"
# The ymd field, not the year+kai+day digits that sit in front of it.
_DATED_CNAME = re.compile(
    r"(?:"
    r"pw01hde\d{2}\d{2}\d{4}\d{2}\d{2}(?P<h>\d{8})"
    r"|pw01dde(?:01|10)\d{2}\d{4}\d{2}\d{2}\d{2}(?P<d>\d{8})"
    r"|pw01drl(?:00|10)\d{2}\d{4}\d{2}\d{2}(?P<m>\d{8})"
    r"|pw01ode\d{2}\d{2}\d{4}\d{2}\d{2}\d{2}(?P<o>\d{8})"
    r"|pw151ou\d{2}\d{2}\d{4}\d{2}\d{2}\d{2}(?P<ou>\d{8})"
    r")"
)


def _refused_cache_body(body: object) -> bool:
    """True when a stored body is a login wall, a 403 page, or a challenge."""
    if not isinstance(body, str) or not body:
        return False
    return looks_like_login_wall(body) or looks_like_forbidden_page(body)


def _legacy_refused(payload: dict[str, object]) -> bool:
    """Check a denial only when this file predates the replayable mark.

    A marked file was accepted at write time. Parsing it again on every read
    walks the whole document.
    """
    if isinstance(payload.get("replayable"), bool):
        return False
    return _refused_cache_body(payload.get("body"))


def _result_replayable(url: str, body: str) -> bool:
    """A result page is replayable only when it is confirmed and complete.

    A flash or short table fails first. An unsettled page and a provisional
    label fail the finality check. The odds API is replayable only when
    ``status`` is ``result``, the odds map is non-empty, and
    ``official_datetime`` is not today or later. ``odds/index.html`` is
    replayable only when a price was extracted. Other URLs are replayable
    here; the live-list, live-JRA, and track checks are separate.
    """
    if _ODDS_API in url:
        return odds_api_is_replayable(body)
    if "odds/index.html" in url:
        return _has_price_text(body)
    if "result.html" not in url:
        return True
    if result_html_is_partial(body):
        return False
    return result_html_is_final(body)


def _request_text(url: str, params: dict | None) -> str:
    parts = [url]
    if params:
        parts.extend(str(value) for value in params.values())
    return " ".join(parts)


def _cname_meeting_day(url: str, params: dict | None) -> date | None:
    """The ymd printed in an accessH, accessO, or accessD cname."""
    match = _DATED_CNAME.search(_request_text(url, params))
    if match is None:
        return None
    raw = next(group for group in match.groups() if group)
    day, warning = parse_race_day(raw)
    if day is None or warning:
        return None
    return day


def jra_live_request(url: str, params: dict | None = None, *, today: date | None = None) -> bool:
    """True when this JRA page is for today or later, or has no meeting date.

    A past ``ymd`` may stay cached. An undated menu is the live card, so it
    is not stored. The odds API is not decided here.
    """
    if not any(marker in url for marker in _JRA_LIVE_URLS):
        return False
    day = _cname_meeting_day(url, params)
    if day is None:
        return True
    current = today or tokyo_now().date()
    return day >= current


def _kaisai_date_text(url: str, params: dict | None) -> str | None:
    if params:
        raw = params.get("kaisai_date")
        if raw:
            return str(raw)
    values = parse_qs(urlparse(url).query).get("kaisai_date") or []
    if values:
        return str(values[0])
    return None


def live_race_list(url: str, params: dict | None = None, *, today: date | None = None) -> bool:
    """True when this race list is for today or later in Asia/Tokyo.

    An undated list is live. A past ``kaisai_date`` may stay cached.
    """
    if not any(marker in url for marker in _RACE_LIST_URLS):
        return False
    raw = _kaisai_date_text(url, params)
    if raw is None:
        return True
    day, warning = parse_race_day(raw)
    if day is None or warning:
        return True
    current = today or tokyo_now().date()
    return day >= current


def default_page_cache_dir() -> Path:
    """backend/.cache/pages. The directory is created on the first write."""
    return Path(__file__).resolve().parents[2] / ".cache" / "pages"


class CachingHTTPClient(HTTPClient):
    """HTTP client that stores successful public pages on disk.

    A cache hit returns the stored body and does not wait. A miss uses the
    normal rate limit, then stores the body with the time it was fetched.
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        *,
        transport: object | None = None,
        min_request_interval: float = 1.5,
    ):
        super().__init__(min_request_interval=min_request_interval)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else default_page_cache_dir()
        self._transport = transport
        # Legacy files have no replayable mark. Remember the judgement per path and mtime.
        self._replay_cache: dict[str, tuple[int, bool]] = {}
        # Set for a race whose meeting day is today or later. Odds pages are not stored.
        self.replay_disabled = False

    def cached_fetched_at(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        data: dict | None = None,
    ) -> datetime | None:
        """When this URL was fetched. None when it is not a replayable page."""
        if (
            should_bypass_page_cache(url, params)
            or live_race_list(url, params)
            or jra_live_request(url, params)
        ):
            return None
        path = self._path(self._cache_key(method, url, params, data))
        payload = self._load(path)
        if payload is None or _legacy_refused(payload):
            return None
        body = payload.get("body")
        if not isinstance(body, str) or not body:
            return None
        if not self._replayable(path, url, payload, body):
            return None
        raw = payload.get("fetched_at")
        if not isinstance(raw, str):
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        encoding: str | None = None,
    ) -> str:
        key = self._cache_key("GET", url, params, None)
        if self._may_read_cache(url, params):
            cached = self._cached_body(key, url)
            if cached is not None:
                return cached
        body = self._fetch("get", url, params=params, data=None, headers=headers, encoding=encoding)
        if self._may_read_cache(url, params) and _result_replayable(url, body):
            self._store(key, url, body)
        return body

    def post(
        self,
        url: str,
        data: dict | None = None,
        headers: dict | None = None,
        encoding: str | None = None,
    ) -> str:
        key = self._cache_key("POST", url, None, data)
        if self._may_read_cache(url, data):
            cached = self._cached_body(key, url)
            if cached is not None:
                return cached
        body = self._fetch("post", url, params=None, data=data, headers=headers, encoding=encoding)
        if self._may_read_cache(url, data) and _result_replayable(url, body):
            self._store(key, url, body)
        return body

    def _fetch(
        self,
        method: str,
        url: str,
        *,
        params: dict | None,
        data: dict | None,
        headers: dict | None,
        encoding: str | None,
    ) -> str:
        if self._transport is None:
            if method == "post":
                return super().post(url, data=data, headers=headers, encoding=encoding)
            return super().get(url, params=params, headers=headers, encoding=encoding)
        self._wait_for_rate_limit()
        caller = getattr(self._transport, method)
        if method == "post":
            body = caller(url, data=data, headers=headers, encoding=encoding)
        else:
            body = caller(url, params=params, headers=headers, encoding=encoding)
        if not isinstance(body, str):
            raise TypeError("http transport did not return text")
        self._last_request_time = datetime.now().timestamp()
        return body

    def _may_read_cache(self, url: str, params: dict | None) -> bool:
        """False for a live track page, today's race list, today's JRA page, or a disabled replay."""
        if self.replay_disabled:
            return False
        return (
            not should_bypass_page_cache(url, params)
            and not live_race_list(url, params)
            and not jra_live_request(url, params)
        )

    def _cached_body(self, key: str, url: str) -> str | None:
        """A stored body. A non-final result page is not replayed.

        A non-result page this client stored is marked replayable, so a later
        read does not parse it for a denial or for completeness. ``result.html``,
        the odds API, and ``odds/index.html`` are judged from the body, once per
        path and mtime, even when that mark is set. A result page must be
        confirmed and complete. The odds API must have ``status`` ``result``.
        An odds index must contain a price.
        """
        path = self._path(key)
        payload = self._load(path)
        if payload is None:
            return None
        body = payload.get("body")
        if not isinstance(body, str) or not body:
            return None
        if _legacy_refused(payload):
            return None
        if not self._replayable(path, url, payload, body):
            return None
        return body

    def _replayable(self, path: Path, url: str, payload: dict[str, object], body: str) -> bool:
        """False when a result page is not confirmed and complete.

        A non-result page with a replayable mark is kept without parsing.
        ``result.html``, the odds API, and ``odds/index.html`` are always
        judged from the body, mark or not.
        """
        if "result.html" in url or _ODDS_API in url or "odds/index.html" in url:
            return self._body_is_complete(path, url, body)
        marked = payload.get("replayable")
        if isinstance(marked, bool):
            return marked
        return self._body_is_complete(path, url, body)

    def _body_is_complete(self, path: Path, url: str, body: str) -> bool:
        """True when this file may be replayed.

        A result page must be confirmed and complete. That rejects a flash,
        a short order, an unsettled page, and a provisional label. The odds
        API must report ``result``. The judgement is remembered for the path
        and mtime. A new write changes the mtime, so the next read checks the
        new body.
        """
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return _result_replayable(url, body)
        cached = self._replay_cache.get(str(path))
        if cached is not None and cached[0] == mtime:
            return cached[1]
        ok = _result_replayable(url, body)
        self._replay_cache[str(path)] = (mtime, ok)
        return ok

    def _store(self, key: str, url: str, body: str) -> None:
        if (
            not body
            or looks_like_login_wall(body)
            or looks_like_forbidden_page(body)
            or not _result_replayable(url, body)
        ):
            return
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "url": url,
            "fetched_at": tokyo_now().isoformat(timespec="seconds"),
            "replayable": True,
            "body": body,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self._replay_cache.pop(str(path), None)

    def _load(self, path: Path) -> dict[str, object] | None:
        if not path.is_file():
            return None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Page cache %s could not be read: %s", path, exc)
            return None
        if not isinstance(loaded, dict):
            return None
        return loaded

    def _path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    @staticmethod
    def _cache_key(
        method: str,
        url: str,
        params: dict | None,
        data: dict | None,
    ) -> str:
        raw = json.dumps(
            {"method": method, "url": url, "params": params or {}, "data": data or {}},
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
