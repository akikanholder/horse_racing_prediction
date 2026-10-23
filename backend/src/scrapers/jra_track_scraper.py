"""JRA baba (track condition) scraper.

Source: https://www.jra.go.jp/keiba/baba/index.html
"""

import logging
import re
from collections.abc import Callable
from datetime import date, datetime
from typing import Literal
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Comment, Tag

from backend.models.track import (
    CourseInfo,
    CourseType,
    CushionValue,
    GrassInfo,
    MoistureContent,
    TrackCondition,
    TrackInfo,
    Weather,
    WeeklyInfo,
    empty_track_info,
)
from backend.scrapers.base_scraper import BaseScraper
from backend.scrapers.jra_headless import HeadlessTransport
from backend.scrapers.page_fallback import (
    FetchedBody,
    PageFallback,
    looks_like_login_wall,
    page_block_reason,
)
from backend.scrapers.track_freshness import (
    CACHED_COPY,
    FRESH_REQUEST_HEADERS,
    apply_measurement_freshness,
    cached_copy_seen,
    clear_cached_copy_flag,
    measurement_times,
    page_measurement_time,
)
from backend.scrapers.track_readings import (
    CUSHION_MISSING,
    PrintedMoisture,
    cushion_from_html,
    cushion_from_json,
    discover_jra_baba_urls,
    fetch_or_block,
    going_label,
    join_warnings,
    json_object,
    moisture_pair,
    parse_cushion_cell,
    post_time_on_page,
    printed_float,
    printed_year,
    weather_label,
)
from backend.utils.date_utils import normalize_tokyo_clock, parse_measurement_time, tokyo_now

logger = logging.getLogger(__name__)

_INDEX_TAB = re.compile(r"^index\d*\.html$", re.IGNORECASE)
_MEASUREMENT_SECTIONS = ("馬場状態", "クッション値", "含水率")
# These names end the open section at any heading level, and also as a row label.
_SECTION_STOPS = ("使用コース", "芝コースの様子", "芝の状態", "散水情報")
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_HEADING_LEVEL = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
# A plain-text section name is the same level as the live page's h2 header.
_TEXT_SECTION_LEVEL = 2
CUSHION_UNDATED = "cushion reading has no measurement time and is not current"
MOISTURE_UNDATED = "moisture reading has no measurement time and is not current"
_UNUSED_FETCHED_AT = datetime(1970, 1, 1)


def measurement_sections(text: str) -> dict[str, str]:
    """Text of the 馬場状態, cushion, and moisture sections.

    週間情報 is a weather note. Its clock, including ``8時現在``, is not a
    track measurement. A known section name ends the open section at any
    heading level, and also when that name is a row or a label rather than a
    heading tag. Those names are 週間情報, 使用コース, 芝の状態, and 散水情報.
    An ``h3`` 週間情報 under an ``h2`` therefore leaves 含水率. An unknown
    heading ends the section only when it is the same level or higher. An
    ``h4`` 測定時刻 inside クッション値 or 含水率 stays in the section, and so
    does the dropdown under it. A data cell that merely contains 計測情報 is
    not a heading. Clocks are collected only from 馬場状態, クッション値, and
    含水率.
    """
    buckets: dict[str, list[str]] = {}
    current: str | None = None
    current_level = _TEXT_SECTION_LEVEL
    for line in text.splitlines():
        opened = _section_heading(line)
        if opened is not None:
            name, level = opened
            nested = current in _MEASUREMENT_SECTIONS and name == "other" and level > current_level
            if nested:
                buckets.setdefault(current, []).append(_section_visible(line))
                continue
            current = name
            current_level = level
            if name not in _MEASUREMENT_SECTIONS:
                continue
        if current in _MEASUREMENT_SECTIONS:
            buckets.setdefault(current, []).append(_section_visible(line))
    return {name: "\n".join(lines) for name, lines in buckets.items()}


def measurement_sections_from_soup(soup: BeautifulSoup) -> dict[str, str]:
    """Section text from the document. Heading tags carry their level."""
    return measurement_sections("\n".join(_section_lines(soup)))


def _section_lines(soup: BeautifulSoup) -> list[str]:
    """Visible lines. A heading tag is one line, marked with its level."""
    lines: list[str] = []
    emitted: set[int] = set()
    for node in soup.find_all(string=True):
        if isinstance(node, Comment):
            continue
        parent = node.parent
        if not isinstance(parent, Tag):
            continue
        if parent.name in {"script", "style", "title"} or parent.find_parent(
            ["script", "style", "title"]
        ):
            continue
        text = str(node).strip()
        if not text:
            continue
        heading = parent if parent.name in _HEADING_TAGS else parent.find_parent(_HEADING_TAGS)
        if isinstance(heading, Tag):
            if id(heading) in emitted:
                continue
            emitted.add(id(heading))
            title = heading.get_text(" ", strip=True)
            if title:
                level = _HEADING_LEVEL.get(str(heading.name), _TEXT_SECTION_LEVEL)
                lines.append(f"§{level}\t{title}")
            continue
        lines.append(text)
    return lines


def _section_visible(line: str) -> str:
    """Drop the heading marker. The title itself can still hold a clock."""
    if line.startswith("§"):
        _level, _sep, title = line.partition("\t")
        return title if title else line[1:]
    return line


def _section_heading(line: str) -> tuple[str, int] | None:
    """Section a line opens, and the heading level that opened it.

    A ``§`` prefix marks an HTML heading element. A known section name ends
    the previous section even when the heading is deeper, and even when the
    name is only a row label. An unlisted title at the same level or higher
    still ends the previous section. A word in a data row, such as 計測情報,
    does not. Clocks are kept only for the three measurement sections.
    """
    raw = line.strip()
    if raw.startswith("§"):
        level_text, _sep, title = raw[1:].partition("\t")
        try:
            level = int(level_text)
        except ValueError:
            level = _TEXT_SECTION_LEVEL
            title = raw[1:]
        compact = re.sub(r"\s+", "", title)
        if not compact:
            return None
        named = _named_section(compact)
        return (named if named is not None else "other", level)
    compact = re.sub(r"\s+", "", raw)
    if not compact:
        return None
    named = _named_section(compact)
    if named is not None:
        return named, _TEXT_SECTION_LEVEL
    return None


def _named_section(compact: str) -> str | None:
    """Known section name, or None when this line is not one of them.

    A stop such as 散水情報 matches a heading and a row label. It does not
    match a sentence that merely mentions 散水 later in the line. 週間情報
    matches anywhere in the line, including an ``h3`` under an ``h2``.
    """
    if "週間情報" in compact:
        return "週間情報"
    if "クッション値" in compact:
        return "クッション値"
    if "含水率" in compact:
        return "含水率"
    if compact.startswith("馬場状態"):
        return "馬場状態"
    for name in _SECTION_STOPS:
        if compact == name or compact.startswith(name):
            return name
    return None


def baba_meeting_tab_urls(html: str, page_url: str) -> list[str]:
    """``indexN.html`` links inside ``.kaisai_tab``.

    A third meeting is ``index3.html`` on that tab. Facility pages and
    ``/keiba/baba/{venue}.html`` are not followed.
    """
    soup = BeautifulSoup(html or "", "lxml")
    found: list[str] = []
    seen: set[str] = set()
    for link in soup.select(".kaisai_tab a[href]"):
        href = str(link.get("href") or "").strip()
        if not href:
            continue
        joined = urljoin(page_url, href)
        parsed = urlparse(joined)
        name = parsed.path.rsplit("/", 1)[-1]
        if _INDEX_TAB.fullmatch(name) is None:
            continue
        clean = urlunparse(parsed._replace(query="", fragment=""))
        if clean in seen:
            continue
        seen.add(clean)
        found.append(clean)
    return found


class JRATrackScraper(BaseScraper):
    """Scraper for JRA track (baba) information."""

    BASE_URL = "https://www.jra.go.jp"
    TRACK_INFO_PATH = "/keiba/baba/index.html"

    RACECOURSE_MAPPING = {
        "nakayama": "中山",
        "tokyo": "東京",
        "kyoto": "京都",
        "hanshin": "阪神",
        "sapporo": "札幌",
        "hakodate": "函館",
        "fukushima": "福島",
        "niigata": "新潟",
        "chukyo": "中京",
        "kokura": "小倉",
    }
    # Direct /keiba/baba/{venue}.html URLs often return 403. The current
    # meetings are the two index tabs, encoded cp932.
    BABA_INDEX_URLS = (
        "https://www.jra.go.jp/keiba/baba/index.html",
        "https://www.jra.go.jp/keiba/baba/index2.html",
    )
    BABA_ENCODING = "cp932"
    BABA_403 = "JRA baba returned 403"
    BABA_CAPTCHA = "JRA baba returned a captcha"
    BABA_CHALLENGE = "JRA baba returned a challenge"
    BABA_NOT_MEETING = "JRA baba page is not the requested meeting"
    BABA_UNPARSEABLE = "JRA baba page could not be parsed"
    BABA_FETCH_FAILED = "JRA baba fetch failed"
    BABA_LOGIN = "JRA baba requires login"
    CUSHION_MISSING = CUSHION_MISSING
    CUSHION_UNDATED = CUSHION_UNDATED
    MOISTURE_UNDATED = MOISTURE_UNDATED

    def __init__(
        self,
        http_client: object | None = None,
        *,
        headless_render: Callable[[str], str] | Literal["auto", "off"] = "auto",
        fetch_mode: Literal["html", "headless", "auto"] = "html",
        headless_session: object | None = None,
    ):
        """Create a scraper.

        ``headless_render="off"`` skips the browser step and does not warn.
        ``"auto"`` uses Playwright when the browser is installed.
        ``fetch_mode="html"`` keeps the HTTP path. A refusal is not fetched again.
        """
        super().__init__(http_client)  # type: ignore[arg-type]
        self.fetch_mode: Literal["html", "headless", "auto"] = fetch_mode
        self._transport = HeadlessTransport(
            self.http_client,
            mode_of=lambda: self.fetch_mode,
            session=headless_session,
        )
        self.http_client = self._transport  # type: ignore[assignment]
        self._headless_render = headless_render

    def get_base_url(self) -> str:
        """Return the base URL for JRA."""
        return self.BASE_URL

    def get_track_url(self, racecourse: str | None = None) -> str:
        """Get the URL for track information.

        Args:
            racecourse: Racecourse code (e.g., 'nakayama', 'tokyo')
                       If None, returns the index page URL

        Returns:
            URL for the track information page
        """
        if racecourse:
            return urljoin(self.BASE_URL, f"/keiba/baba/{racecourse}.html")
        return urljoin(self.BASE_URL, self.TRACK_INFO_PATH)

    def scrape(
        self,
        racecourse: str | None = None,
        *,
        fetched_at: datetime | None = None,
        race_date: date | None = None,
        today: date | None = None,
        race_date_warning: str | None = None,
    ) -> TrackInfo:
        """Scrape track information from the current JRA baba tabs.

        Direct venue URLs are not requested. A 403, a non-page, or a tab for
        a different meeting returns TrackInfo with null racecourse, cushion,
        and moisture plus ``warning``. A 403 is not retried and is not evaded.

        When ``#cushion_num`` is empty, or a printed cushion or moisture number
        has no measurement time and the page loads ``_data_cushion.html`` or
        ``_data_moist.html``, that same-site file is read next. A dated reading
        replaces the undated static numbers. Headless runs only when that chain
        is already open and the time is still missing. The hardness scale is
        not the cushion value. Undated numbers stay, with ``measured_at`` null
        and a warning. They are not the current measurement.

        Args:
            racecourse: Racecourse code (e.g., 'nakayama', 'tokyo') or a
                       Japanese name. If None, uses the first tab whose
                       title names a meeting.
            fetched_at: Timestamp stored on each copied value. Defaults to now
                       in Asia/Tokyo.
            race_date: Day the reading is for. Null means today in Asia/Tokyo.
            today: Clock date used for ``is_today``. Defaults to today in Asia/Tokyo.
            race_date_warning: Set when the caller could not read the race date.
                ``stale`` stays null. The comparison day is not invented.

        Returns:
            TrackInfo object with the scraped data, or an empty one
        """
        fetched = fetched_at or tokyo_now()
        self._transport.fallback_warning = None
        clear_cached_copy_flag(self.http_client)
        requested = self._requested_name(racecourse)
        if racecourse and requested is None:
            return empty_track_info("JRA", self.BABA_UNPARSEABLE)
        (
            pages,
            sources,
            saw_403,
            saw_http_403,
            saw_error,
            saw_login,
            saw_captcha,
            saw_challenge,
        ) = self._load_meeting_tabs(requested)

        chosen = self._choose_page(pages, requested)
        if chosen is None:
            htmls = [html for _, html in pages]
            warning = self._miss_warning(
                htmls, saw_403, saw_error, saw_login, saw_captcha, saw_challenge
            )
            return self._finish(
                self._with_access(
                    empty_track_info("JRA", warning),
                    warning=warning,
                    saw_403=saw_403,
                    saw_http_403=saw_http_403,
                    saw_login=saw_login,
                    saw_captcha=saw_captcha,
                    saw_challenge=saw_challenge,
                ),
                race_date=race_date,
                today=today,
                race_date_warning=race_date_warning,
            )
        url, html = chosen
        venue = self._title_venue(html) or requested
        if venue is None:
            return self._finish(
                empty_track_info("JRA", self.BABA_UNPARSEABLE),
                race_date=race_date,
                today=today,
                race_date_warning=race_date_warning,
            )
        loaded = self._enrich(url, html, fetched, document_source=sources.get(url))
        if not loaded.pages:
            return self._finish(
                self._with_fetch_block(
                    empty_track_info("JRA", self._blocked_warning(loaded)), loaded
                ),
                race_date=race_date,
                today=today,
                race_date_warning=race_date_warning,
            )
        info = self._from_pages(loaded.pages, venue)
        if loaded.status == "blocked":
            extra = join_warnings(self._transport.fallback_warning, self._blocked_warning(loaded))
            info = self._with_fetch_block(info, loaded)
        else:
            extra = join_warnings(loaded.warning, self._transport.fallback_warning)
        if extra:
            info = info.model_copy(update={"warning": join_warnings(info.warning, extra)})
        return self._finish(
            info,
            race_date=race_date,
            today=today,
            race_date_warning=race_date_warning,
        )

    def _finish(
        self,
        info: TrackInfo,
        *,
        race_date: date | None,
        today: date | None,
        race_date_warning: str | None = None,
    ) -> TrackInfo:
        """Attach a cache-hit note, then the printed measurement clock."""
        if cached_copy_seen(self.http_client):
            info = info.model_copy(update={"warning": join_warnings(info.warning, CACHED_COPY)})
        return apply_measurement_freshness(
            info,
            race_date=race_date,
            today=today,
            race_date_warning=race_date_warning,
        )

    def _load_meeting_tabs(
        self, requested: str | None
    ) -> tuple[list[tuple[str, str]], dict[str, str], bool, bool, bool, bool, bool, bool]:
        """The two known tabs, then further ``indexN.html`` links when needed.

        A requested meeting that is already on ``index.html`` or ``index2.html``
        does not open another tab. When it is on neither, tabs named inside
        ``.kaisai_tab`` are fetched. Direct venue URLs are not requested.
        """
        pages: list[tuple[str, str]] = []
        sources: dict[str, str] = {}
        saw_403 = False
        saw_http_403 = False
        saw_error = False
        saw_login = False
        saw_captcha = False
        saw_challenge = False
        seen: set[str] = set()

        def fetch(url: str) -> str | None:
            nonlocal saw_403, saw_http_403, saw_error, saw_login, saw_captcha, saw_challenge
            if url in seen or saw_login or saw_captcha or saw_challenge:
                return None
            seen.add(url)
            try:
                html = self.http_client.get(
                    url, encoding=self.BABA_ENCODING, headers=dict(FRESH_REQUEST_HEADERS)
                )
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                reason = getattr(e, "reason", None)
                if reason == "captcha":
                    saw_captcha = True
                elif reason == "challenge":
                    saw_challenge = True
                elif status == 403:
                    saw_403 = True
                    saw_http_403 = True
                elif reason == "http_403":
                    saw_403 = True
                elif status == 401 or reason == "login":
                    saw_login = True
                else:
                    saw_error = True
                logger.info("JRA baba fetch failed for %s (%s)", url, e)
                return None
            if looks_like_login_wall(html):
                saw_login = True
                logger.info("JRA baba requires login for %s", url)
                return None
            blocked = page_block_reason(html)
            if blocked == "captcha":
                saw_captcha = True
                logger.info("JRA baba returned a captcha for %s", url)
                return None
            if blocked == "challenge":
                saw_challenge = True
                logger.info("JRA baba returned a challenge for %s", url)
                return None
            if blocked == "http_403":
                saw_403 = True
                logger.info("JRA baba returned a denied page for %s", url)
                return None
            pages.append((url, html))
            sources[url] = "headless" if self._transport.used_headless else "jra_html"
            return html

        for url in self.BABA_INDEX_URLS:
            fetch(url)
        if requested and self._choose_page(pages, requested) is None:
            extras: list[str] = []
            for url, html in list(pages):
                for extra in baba_meeting_tab_urls(html, url):
                    if extra not in seen and extra not in extras:
                        extras.append(extra)
            for url in extras:
                html = fetch(url)
                if html and self._title_venue(html) == requested:
                    break
        return (
            pages,
            sources,
            saw_403,
            saw_http_403,
            saw_error,
            saw_login,
            saw_captcha,
            saw_challenge,
        )

    def _requested_name(self, racecourse: str | None) -> str | None:
        """Japanese meeting name for a code or name. Unknown codes stay unset."""
        if not racecourse:
            return None
        if racecourse in self.RACECOURSE_MAPPING:
            return self.RACECOURSE_MAPPING[racecourse]
        if racecourse in self.RACECOURSE_MAPPING.values():
            return racecourse
        return None

    def _title_venue(self, html: str) -> str | None:
        """Venue in the baba title. Nav text and 403 pages do not match."""
        soup = BeautifulSoup(html or "", "lxml")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        match = re.search(r"馬場情報（(.+?)競馬場）", title)
        if not match:
            return None
        name = match.group(1).strip()
        if name in self.RACECOURSE_MAPPING.values():
            return name
        return None

    def _miss_warning(
        self,
        pages: list[str],
        saw_403: bool,
        saw_error: bool,
        saw_login: bool = False,
        saw_captcha: bool = False,
        saw_challenge: bool = False,
    ) -> str:
        """Why no tab was copied. A foreign title is not this meeting."""
        if saw_login:
            return self.BABA_LOGIN
        if saw_captcha:
            return self.BABA_CAPTCHA
        if saw_challenge:
            return self.BABA_CHALLENGE
        if any(self._title_venue(html) for html in pages):
            return self.BABA_NOT_MEETING
        if saw_403:
            return self.BABA_403
        if saw_error or not pages:
            return self.BABA_FETCH_FAILED
        return self.BABA_UNPARSEABLE

    def _choose_page(
        self, pages: list[tuple[str, str]], requested: str | None
    ) -> tuple[str, str] | None:
        """The tab whose title is this meeting. Other tabs are not used."""
        if requested:
            for url, html in pages:
                if self._title_venue(html) == requested:
                    return url, html
            return None
        for url, html in pages:
            if self._title_venue(html):
                return url, html
        return None

    def _blocked_warning(self, loaded: object) -> str:
        """Map a stopped chain onto the baba warnings. Do not read the body."""
        reason = getattr(loaded, "blocked_reason", None)
        warning = getattr(loaded, "warning", None)
        if reason == "captcha":
            return warning if isinstance(warning, str) and warning else self.BABA_CAPTCHA
        if reason == "challenge":
            return warning if isinstance(warning, str) and warning else self.BABA_CHALLENGE
        status = getattr(loaded, "http_status", None)
        login = bool(getattr(loaded, "login_required", False))
        if reason == "http_403" or (status == 403 and not login):
            return self.BABA_403
        if reason == "login" or login or status == 401:
            return self.BABA_LOGIN
        return self.BABA_FETCH_FAILED

    def _with_access(
        self,
        info: TrackInfo,
        *,
        warning: str,
        saw_403: bool,
        saw_login: bool,
        saw_http_403: bool = False,
        saw_captcha: bool = False,
        saw_challenge: bool = False,
    ) -> TrackInfo:
        """Set the refusal flags only when that refusal is why the meeting was missed.

        A 403 on one tab does not mark the result blocked when another tab names
        a venue. The warning is then "not this meeting", and ``http_status`` stays null.
        Captcha and challenge set ``blocked_reason`` and leave ``http_status`` null.
        """
        if warning == self.BABA_LOGIN and saw_login:
            return info.model_copy(
                update={
                    "http_status": 401,
                    "login_required": True,
                    "blocked_reason": "login",
                }
            )
        if warning == self.BABA_CAPTCHA and saw_captcha:
            return info.model_copy(
                update={"http_status": None, "login_required": False, "blocked_reason": "captcha"}
            )
        if warning == self.BABA_CHALLENGE and saw_challenge:
            return info.model_copy(
                update={
                    "http_status": None,
                    "login_required": False,
                    "blocked_reason": "challenge",
                }
            )
        if warning == self.BABA_403 and saw_403:
            return info.model_copy(
                update={
                    "http_status": 403 if saw_http_403 else None,
                    "login_required": False,
                    "blocked_reason": "http_403",
                }
            )
        return info

    def _with_fetch_block(self, info: TrackInfo, loaded: object) -> TrackInfo:
        """Copy a fallback refusal onto the track info. Text in the body is not a status."""
        status = getattr(loaded, "http_status", None)
        login = bool(getattr(loaded, "login_required", False))
        reason = getattr(loaded, "blocked_reason", None)
        if (
            reason not in {"captcha", "challenge", "login", "http_403"}
            and status
            not in {
                401,
                403,
            }
            and not login
        ):
            return info
        update: dict[str, object] = {}
        if reason in {"captcha", "challenge", "login", "http_403"}:
            update["blocked_reason"] = reason
        if reason in {"captcha", "challenge"}:
            update["http_status"] = None
            update["login_required"] = False
        elif status in {401, 403} or login:
            update["http_status"] = status
            update["login_required"] = login
        if not update:
            return info
        return info.model_copy(update=update)

    def _enrich(
        self,
        url: str,
        html: str,
        fetched_at: datetime,
        *,
        document_source: str | None = None,
    ) -> object:
        """Follow the page's own data files when a reading has no measurement time."""
        fallback = PageFallback(
            self.http_client,
            site="jra",
            headless_render=self._headless_render,
            url_finder=self._baba_urls,
        )
        return fallback.load(
            url=url,
            params=None,
            headers=dict(FRESH_REQUEST_HEADERS),
            encoding=self.BABA_ENCODING,
            fetched_at=fetched_at,
            should_fallback=self._needs_dated_source,
            still_incomplete=self._still_needs_dated_source,
            html_body=html,
            document_source=document_source,
        )

    def _baba_urls(self, html: str, page_url: str) -> list[str]:
        """``_data_cushion.html`` and ``_data_moist.html`` only."""

        def fetch(script_url: str) -> str:
            return fetch_or_block(
                self.http_client.get,
                script_url,
                encoding=self.BABA_ENCODING,
                headers=dict(FRESH_REQUEST_HEADERS),
            )

        return _select_baba_urls(html, discover_jra_baba_urls(html, page_url, fetch), self)

    def _cushion_slot_empty(self, html: str) -> bool:
        """True when the page has a cushion slot and the number is not in it."""
        reading = cushion_from_html(BeautifulSoup(html, "lxml"))
        return reading.slot and reading.value is None

    def _needs_dated_source(self, html: str) -> bool:
        """True when a same-site data file, or the empty cushion slot, should be read.

        Undated static numbers with no ``_data_cushion.html`` / ``_data_moist.html``
        reference do not open headless. A 403 later in the chain still stops it.
        """
        if self._cushion_slot_empty(html):
            return True
        if not _references_baba_data(html):
            return False
        soup = BeautifulSoup(html, "lxml")
        year = self._page_year(soup)
        return self._cushion_needs_source(html, soup, year) or self._moisture_needs_source(
            html, soup, year
        )

    def _cushion_needs_source(self, html: str, soup: BeautifulSoup, year: int | None) -> bool:
        """True when the cushion number is missing or has no measurement time."""
        if self._cushion_slot_empty(html):
            return True
        cushion = self._extract_cushion_value(soup, "jra_html", _UNUSED_FETCHED_AT, year)
        return cushion is not None and cushion.value is not None and cushion.measured_at is None

    def _moisture_needs_source(self, html: str, soup: BeautifulSoup, year: int | None) -> bool:
        """True when moisture numbers have no time, or the empty slot still needs them."""
        turf_m, dirt_m = self._extract_moisture_content(soup, "jra_html", _UNUSED_FETCHED_AT, year)
        if _moisture_undated(turf_m) or _moisture_undated(dirt_m):
            return True
        if _moisture_value_missing(turf_m) and _moisture_value_missing(dirt_m):
            return self._cushion_slot_empty(html)
        return False

    def _still_needs_dated_source(self, pages: list[FetchedBody]) -> bool:
        """True when headless might still supply a number or a measurement time."""
        venue = self._title_venue(pages[0].body) if pages else None
        if venue is None:
            return False
        info = self._from_pages(pages, venue)
        if _cushion_number(info) is None and self._cushion_slot_empty(pages[0].body):
            return True
        return _undated_reading_warning(info) is not None

    def _from_pages(self, pages: list[FetchedBody], venue: str) -> TrackInfo:
        """Merge the HTML tab with later fragments. Do not invent a number."""
        first = pages[0]
        soup = BeautifulSoup(first.body, "lxml")
        info = self._parse_track_info(soup, venue, source=first.source, fetched_at=first.fetched_at)
        for page in pages[1:]:
            info = self._fill_from_page(info, page, venue)
        if _cushion_number(info) is None:
            info = info.model_copy(
                update={"warning": join_warnings(info.warning, self.CUSHION_MISSING)}
            )
        undated = _undated_reading_warning(info)
        if undated:
            info = info.model_copy(update={"warning": join_warnings(info.warning, undated)})
        return info

    def _fill_from_page(self, info: TrackInfo, page: FetchedBody, venue: str) -> TrackInfo:
        """Prefer a dated cushion or moisture over undated static numbers."""
        payload = json_object(page.body)
        if payload is not None:
            return self._fill_from_json(info, payload, page.source, page.fetched_at)
        soup = BeautifulSoup(page.body, "lxml")
        year = info.date.year if info.date is not None else printed_year(page.body)
        info = self._prefer_cushion_page(info, soup, venue, year, page.source, page.fetched_at)
        turf_m, dirt_m = self._moisture_from_document(
            soup, venue, year, page.source, page.fetched_at
        )
        info = _fill_moisture(info, turf_m, dirt_m)
        return _fill_post_time(info, soup.get_text(" ", strip=True), page.source, page.fetched_at)

    def _prefer_cushion_page(
        self,
        info: TrackInfo,
        soup: BeautifulSoup,
        venue: str,
        year: int | None,
        source: str,
        fetched_at: datetime,
    ) -> TrackInfo:
        """Keep the cushion that has a measurement time when one is printed."""
        if info.turf is None:
            return info
        fragment = self._cushion_from_fragment(soup, venue, year, source, fetched_at)
        extracted = self._extract_cushion_value(soup, source, fetched_at, year)
        incoming = _prefer_cushion(extracted, fragment)
        chosen = _prefer_cushion(info.turf.cushion, incoming)
        if chosen is info.turf.cushion:
            return info
        return info.model_copy(update={"turf": info.turf.model_copy(update={"cushion": chosen})})

    def _moisture_from_document(
        self,
        soup: BeautifulSoup,
        venue: str,
        year: int | None,
        source: str,
        fetched_at: datetime,
    ) -> tuple[MoistureContent | None, MoistureContent | None]:
        """Table cells, then a venue unit. A unit with its own clock wins."""
        table_turf, table_dirt = self._extract_moisture_content(soup, source, fetched_at, year)
        frag_turf, frag_dirt = self._moisture_from_fragment(soup, venue, year, source, fetched_at)
        return _prefer_moisture(table_turf, frag_turf), _prefer_moisture(table_dirt, frag_dirt)

    def _fill_from_json(
        self,
        info: TrackInfo,
        payload: dict[str, object],
        source: str,
        fetched_at: datetime,
    ) -> TrackInfo:
        """Copy JSON keys that are present. A missing key stays null."""
        if _cushion_number(info) is None and info.turf is not None:
            value, grade = cushion_from_json(payload)
            if value is not None or grade is not None:
                info = info.model_copy(
                    update={
                        "turf": info.turf.model_copy(
                            update={
                                "cushion": CushionValue(
                                    value=value,
                                    grade=grade,
                                    source=source,
                                    fetched_at=fetched_at,
                                )
                            }
                        )
                    }
                )
        turf_m = moisture_pair(payload, "goal_front", "fourth_corner")
        dirt_m = moisture_pair(payload, "dirt_goal_front", "dirt_fourth_corner")
        if turf_m.goal_front is not None or turf_m.fourth_corner is not None:
            turf_m = PrintedMoisture(
                goal_front=turf_m.goal_front,
                fourth_corner=turf_m.fourth_corner,
            )
        info = _apply_moisture(info, turf_m, dirt_m, source, fetched_at)
        turf_going = going_label(payload, "going") or going_label(payload, "turf_going")
        dirt_going = going_label(payload, "dirt_going")
        info = _apply_going(info, turf_going, dirt_going, source, fetched_at)
        weather = weather_label(payload)
        if weather and info.weather == Weather.UNKNOWN:
            parsed = Weather.from_string(weather)
            if parsed != Weather.UNKNOWN:
                info = info.model_copy(
                    update={
                        "weather": parsed,
                        "weather_source": source,
                        "weather_fetched_at": fetched_at,
                    }
                )
        return _fill_post_time(info, _json_post_time(payload), source, fetched_at)

    def _parse_track_info(
        self,
        soup: BeautifulSoup,
        racecourse_name: str,
        *,
        source: str,
        fetched_at: datetime,
    ) -> TrackInfo:
        """Parse a page already matched to this meeting. Do not invent readings."""
        race_date = self._printed_date(soup)
        year = race_date.year if race_date is not None else printed_year(soup.get_text())
        weather = self._extract_weather(soup)
        turf_condition = self._extract_track_condition(soup, CourseType.TURF)
        dirt_condition = self._extract_track_condition(soup, CourseType.DIRT)
        cushion_value = self._extract_cushion_value(soup, source, fetched_at, year)
        turf_moisture, dirt_moisture = self._extract_moisture_content(
            soup, source, fetched_at, year
        )
        post = post_time_on_page(soup.get_text(" ", strip=True))
        sections = measurement_sections_from_soup(soup)
        measured_at = page_measurement_time(
            "\n".join(sections.get(name, "") for name in _MEASUREMENT_SECTIONS),
            year,
        )

        turf_info = CourseInfo(
            course_type=CourseType.TURF,
            condition=turf_condition,
            condition_source=source if turf_condition != TrackCondition.UNKNOWN else None,
            condition_fetched_at=fetched_at if turf_condition != TrackCondition.UNKNOWN else None,
            moisture=turf_moisture,
            cushion=cushion_value,
        )
        dirt_info = CourseInfo(
            course_type=CourseType.DIRT,
            condition=dirt_condition,
            condition_source=source if dirt_condition != TrackCondition.UNKNOWN else None,
            condition_fetched_at=fetched_at if dirt_condition != TrackCondition.UNKNOWN else None,
            moisture=dirt_moisture,
        )
        return TrackInfo(
            racecourse=racecourse_name,
            date=race_date,
            weather=weather,
            weather_source=source if weather != Weather.UNKNOWN else None,
            weather_fetched_at=fetched_at if weather != Weather.UNKNOWN else None,
            turf=turf_info,
            dirt=dirt_info,
            grass_info=self._extract_grass_info(soup),
            weekly_info=self._extract_weekly_info(soup),
            source="JRA",
            measured_at=measured_at,
            warning=None,
            post_time=post,
            post_time_source=source if post else None,
            post_time_fetched_at=fetched_at if post else None,
        )

    def _printed_date(self, soup: BeautifulSoup) -> datetime | None:
        """Meeting date when the page prints a year. Otherwise null."""
        match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", soup.get_text())
        if match is None:
            return None
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))

    def _page_year(self, soup: BeautifulSoup) -> int | None:
        """Year printed on the page. A page with no year stays unset."""
        printed = self._printed_date(soup)
        if printed is not None:
            return printed.year
        return printed_year(soup.get_text(" ", strip=True))

    def _extract_weather(self, soup: BeautifulSoup) -> Weather:
        """Extract the weather condition from the page."""
        weather_patterns = [
            (r"天候[：:]\s*([^\s<]+)", None),
            (r"天候", "next_sibling"),
        ]

        text = soup.get_text()
        for pattern, method in weather_patterns:
            match = re.search(pattern, text)
            if match:
                weather_str = match.group(1) if match.lastindex else ""
                return Weather.from_string(weather_str)

        weather_elem = soup.find(string=re.compile(r"天候"))
        if weather_elem:
            parent = weather_elem.parent
            if parent:
                next_text = parent.get_text()
                for w in Weather:
                    if w.value in next_text:
                        return w

        return Weather.UNKNOWN

    def _extract_track_condition(
        self, soup: BeautifulSoup, course_type: CourseType
    ) -> TrackCondition:
        """Extract track condition for a specific course type."""
        course_name = course_type.value

        text = soup.get_text()
        pattern = rf"{course_name}[^\n]*?([良稍重不良]+)"
        match = re.search(pattern, text)
        if match:
            return TrackCondition.from_string(match.group(1))

        sections = soup.find_all(["h4", "h5", "dt", "th"])
        for section in sections:
            if course_name in section.get_text():
                next_elem = section.find_next(["dd", "td", "p", "span"])
                if next_elem:
                    for cond in TrackCondition:
                        if cond.value in next_elem.get_text():
                            return cond

        return TrackCondition.UNKNOWN

    def _extract_cushion_value(
        self,
        soup: BeautifulSoup,
        source: str,
        fetched_at: datetime,
        year: int | None,
    ) -> CushionValue | None:
        """Cushion number printed in the value slot. The hardness scale is ignored."""
        reading = cushion_from_html(soup)
        if reading.value is None and reading.grade is None:
            return None
        measured_at = self._extract_measurement_time(soup, "クッション値", year)
        return CushionValue(
            value=reading.value,
            grade=reading.grade,
            measured_at=measured_at,
            source=source,
            fetched_at=fetched_at,
        )

    def _extract_moisture_content(
        self,
        soup: BeautifulSoup,
        source: str,
        fetched_at: datetime,
        year: int | None,
    ) -> tuple[MoistureContent | None, MoistureContent | None]:
        """Moisture printed in the goal / 4th-corner cells. The legend is ignored."""
        turf_moisture = None
        dirt_moisture = None
        measured_at = self._extract_measurement_time(soup, "含水率", year)

        tables = soup.find_all("table")
        for table in tables:
            if table.get("id") == "cushion_relation":
                continue
            text = table.get_text()
            if "ゴール前" not in text or "4コーナー" not in text:
                continue
            if "馬場状態" in text and "稍重" in text and "芝" not in text:
                continue
            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all(["td", "th"])
                if len(cells) < 3:
                    continue
                label = cells[0].get_text().strip()
                goal_front = printed_float(cells[1].get_text())
                fourth_corner = printed_float(cells[2].get_text())
                if goal_front is None and fourth_corner is None:
                    continue
                moisture = MoistureContent(
                    goal_front=goal_front,
                    fourth_corner=fourth_corner,
                    measured_at=measured_at,
                    source=source,
                    fetched_at=fetched_at,
                )
                if "芝" in label and "ダート" not in label:
                    turf_moisture = moisture
                elif "ダート" in label:
                    dirt_moisture = moisture

        return turf_moisture, dirt_moisture

    def _cushion_from_fragment(
        self,
        soup: BeautifulSoup,
        venue: str,
        year: int | None,
        source: str,
        fetched_at: datetime,
    ) -> CushionValue | None:
        """Latest cushion unit whose block title is this venue."""
        block = _venue_block(soup, venue)
        if block is None:
            return None
        unit = block.find("div", class_="unit")
        if not isinstance(unit, Tag):
            return None
        cell = unit.find("div", class_="cushion")
        if not isinstance(cell, Tag):
            return None
        value, grade = parse_cushion_cell(cell.get_text(" ", strip=True))
        if value is None and grade is None:
            return None
        stamp = unit.find("div", class_="time")
        measured = None
        if isinstance(stamp, Tag):
            measured = parse_measurement_time(stamp.get_text(" ", strip=True), year)
        return CushionValue(
            value=value,
            grade=grade,
            measured_at=measured,
            source=source,
            fetched_at=fetched_at,
        )

    def _moisture_from_fragment(
        self,
        soup: BeautifulSoup,
        venue: str,
        year: int | None,
        source: str,
        fetched_at: datetime,
    ) -> tuple[MoistureContent | None, MoistureContent | None]:
        """Latest moisture unit for this venue. A printed clock makes this the reading."""
        block = _venue_block(soup, venue)
        if block is None:
            return None, None
        unit = block.find("div", class_="unit")
        if not isinstance(unit, Tag):
            return None, None
        stamp = unit.find("div", class_="time")
        measured = None
        if isinstance(stamp, Tag):
            measured = parse_measurement_time(stamp.get_text(" ", strip=True), year)
        return (
            _span_moisture(unit.find("div", class_="turf"), measured, source, fetched_at),
            _span_moisture(unit.find("div", class_="dirt"), measured, source, fetched_at),
        )

    def _extract_measurement_time(
        self, soup: BeautifulSoup, section_name: str, year: int | None
    ) -> datetime | None:
        """Measurement clock printed in ``section_name``. No year means null.

        週間情報 is not this section. A clock there is not the cushion or
        moisture time.
        """
        text = measurement_sections_from_soup(soup).get(section_name, "")
        times = measurement_times(text, year)
        if times:
            return times[0]
        return None

    def _extract_grass_info(self, soup: BeautifulSoup) -> GrassInfo | None:
        """Extract grass length and course information."""
        text = soup.get_text()

        native_grass = None
        western_grass = None

        native_match = re.search(r"野芝[^\d]*(\d+)[^\d]+(\d+)", text)
        if native_match:
            native_grass = (float(native_match.group(1)), float(native_match.group(2)))

        western_match = re.search(r"洋芝[^\d]*(\d+)[^\d]+(\d+)", text)
        if western_match:
            western_grass = (
                float(western_match.group(1)),
                float(western_match.group(2)),
            )

        course_name = None
        course_match = re.search(r"使用コース[^\n]*?([ABC])コース", text)
        if course_match:
            course_name = f"{course_match.group(1)}コース"

        condition_notes = None
        condition_match = re.search(r"芝の状態[^\n]*\n([^\n]+)", text)
        if condition_match:
            condition_notes = condition_match.group(1).strip()

        if any([native_grass, western_grass, course_name, condition_notes]):
            return GrassInfo(
                native_grass_cm=native_grass,
                western_grass_cm=western_grass,
                course_name=course_name,
                condition_notes=condition_notes,
            )

        return None

    def _extract_weekly_info(self, soup: BeautifulSoup) -> list[WeeklyInfo]:
        """Extract weekly weather information."""
        weekly_info = []

        tables = soup.find_all("table")
        for table in tables:
            headers = table.find_all("th")
            header_texts = [h.get_text().strip() for h in headers]

            if any("日（" in h for h in header_texts):
                rows = table.find_all("tr")
                date_row = None
                weather_row = None
                rainfall_row = None
                watering_row = None

                for row in rows:
                    cells = row.find_all(["td", "th"])
                    if not cells:
                        continue
                    first_cell = cells[0].get_text().strip()

                    if "天候" in first_cell:
                        weather_row = cells[1:]
                    elif "雨量" in first_cell:
                        rainfall_row = cells[1:]
                    elif "散水" in first_cell:
                        watering_row = cells[1:]
                    elif any("日（" in c.get_text() for c in cells):
                        date_row = cells

                if date_row:
                    for i, date_cell in enumerate(date_row):
                        date_text = date_cell.get_text().strip()
                        date_match = re.search(r"(\d{1,2})日", date_text)
                        if date_match:
                            day = int(date_match.group(1))
                            now = datetime.now()
                            date = datetime(now.year, now.month, day)

                            weather = Weather.UNKNOWN
                            if weather_row and i < len(weather_row):
                                weather_text = weather_row[i].get_text().strip()
                                weather = Weather.from_string(weather_text)

                            rainfall = None
                            if rainfall_row and i < len(rainfall_row):
                                rainfall = self._parse_float(rainfall_row[i].get_text())

                            watering = False
                            if watering_row and i < len(watering_row):
                                watering_text = watering_row[i].get_text().strip()
                                watering = "あり" in watering_text

                            weekly_info.append(
                                WeeklyInfo(
                                    date=date,
                                    weather=weather,
                                    rainfall_mm=rainfall,
                                    watering=watering,
                                )
                            )

        return weekly_info

    def _parse_float(self, text: str) -> float | None:
        """Parse a float from text, returning None if not possible."""
        try:
            cleaned = re.sub(r"[^\d.]", "", text)
            if cleaned:
                return float(cleaned)
        except ValueError:
            pass
        return None

    def get_available_racecourses(self) -> list[tuple[str, str]]:
        """Get list of available racecourses.

        Returns:
            List of tuples (code, name)
        """
        return list(self.RACECOURSE_MAPPING.items())


def _cushion_number(info: TrackInfo) -> float | None:
    """Printed cushion number, or null when the slot is empty."""
    if info.turf is None or info.turf.cushion is None:
        return None
    return info.turf.cushion.value


def _venue_block(soup: BeautifulSoup, venue: str) -> Tag | None:
    """The ``rcA`` / ``rcB`` / ``rcC`` block titled with this venue."""
    for block in soup.find_all("div", id=re.compile(r"^rc[ABC]$")):
        title = str(block.get("title") or "").strip()
        if title == venue and isinstance(block, Tag):
            return block
    return None


def _span_moisture(
    node: object,
    measured_at: datetime | None,
    source: str,
    fetched_at: datetime,
) -> MoistureContent | None:
    """Goal and corner spans printed in one turf or dirt unit."""
    if not isinstance(node, Tag):
        return None
    goal = node.find("span", class_="mg")
    corner = node.find("span", class_="m4c")
    goal_front = printed_float(goal.get_text()) if isinstance(goal, Tag) else None
    fourth_corner = printed_float(corner.get_text()) if isinstance(corner, Tag) else None
    if goal_front is None and fourth_corner is None:
        return None
    return MoistureContent(
        goal_front=goal_front,
        fourth_corner=fourth_corner,
        measured_at=measured_at,
        source=source,
        fetched_at=fetched_at,
    )


def _references_baba_data(html: str) -> bool:
    """True when the page itself points at the cushion or moisture fragment."""
    return "_data_cushion.html" in html or "_data_moist.html" in html or "/keiba/baba/_js/" in html


def _select_baba_urls(html: str, urls: list[str], scraper: JRATrackScraper) -> list[str]:
    """Keep the data files that can still supply a missing or undated reading."""
    soup = BeautifulSoup(html, "lxml")
    year = scraper._page_year(soup)
    need_cushion = scraper._cushion_needs_source(html, soup, year)
    need_moisture = scraper._moisture_needs_source(html, soup, year)
    selected: list[str] = []
    for url in urls:
        path = urlparse(url).path
        if path.endswith("_data_cushion.html") and need_cushion:
            selected.append(url)
        elif path.endswith("_data_moist.html") and need_moisture:
            selected.append(url)
    return selected


def _prefer_cushion(
    current: CushionValue | None, incoming: CushionValue | None
) -> CushionValue | None:
    """A dated number replaces an undated one. An undated number does not."""
    if incoming is None or (incoming.value is None and incoming.grade is None):
        return current
    if current is None or current.value is None:
        if incoming.value is not None or current is None:
            return incoming
        return current
    if incoming.measured_at is not None and (
        current.measured_at is None or incoming.measured_at >= current.measured_at
    ):
        return incoming
    return current


def _prefer_moisture(
    current: MoistureContent | None, incoming: MoistureContent | None
) -> MoistureContent | None:
    """A dated moisture reading replaces an undated one. Numbers are not invented."""
    if _moisture_value_missing(incoming):
        return current
    if _moisture_value_missing(current):
        return incoming
    assert incoming is not None and current is not None
    if incoming.measured_at is not None and (
        current.measured_at is None or incoming.measured_at >= current.measured_at
    ):
        return incoming
    return current


def _moisture_value_missing(moisture: MoistureContent | None) -> bool:
    """True when this reading has no goal or corner number."""
    if moisture is None:
        return True
    return moisture.goal_front is None and moisture.fourth_corner is None


def _moisture_undated(moisture: MoistureContent | None) -> bool:
    """True when numbers are printed and no measurement time is attached."""
    if _moisture_value_missing(moisture):
        return False
    assert moisture is not None
    return moisture.measured_at is None


def _undated_reading_warning(info: TrackInfo) -> str | None:
    """Warn when a number has no measurement time. It is not the current reading."""
    notes: list[str] = []
    cushion = info.turf.cushion if info.turf is not None else None
    if cushion is not None and cushion.value is not None and cushion.measured_at is None:
        notes.append(CUSHION_UNDATED)
    undated_moisture = False
    for course in (info.turf, info.dirt):
        if course is not None and _moisture_undated(course.moisture):
            undated_moisture = True
    if undated_moisture:
        notes.append(MOISTURE_UNDATED)
    return join_warnings(*notes)


def _fill_moisture(
    info: TrackInfo,
    turf: MoistureContent | None,
    dirt: MoistureContent | None,
) -> TrackInfo:
    """Prefer a dated moisture reading. Undated numbers fill only an empty course."""
    turf_update = _course_moisture(info.turf, turf)
    dirt_update = _course_moisture(info.dirt, dirt)
    if turf_update is info.turf and dirt_update is info.dirt:
        return info
    return info.model_copy(update={"turf": turf_update, "dirt": dirt_update})


def _course_moisture(
    course: CourseInfo | None, incoming: MoistureContent | None
) -> CourseInfo | None:
    """Copy ``incoming`` onto ``course`` when it is the reading to keep."""
    if course is None or _moisture_value_missing(incoming):
        return course
    chosen = _prefer_moisture(course.moisture, incoming)
    if chosen is course.moisture:
        return course
    return course.model_copy(update={"moisture": chosen})


def _apply_moisture(
    info: TrackInfo,
    turf: PrintedMoisture,
    dirt: PrintedMoisture,
    source: str,
    fetched_at: datetime,
) -> TrackInfo:
    """Copy JSON moisture when that course has none yet."""

    def build(reading: PrintedMoisture) -> MoistureContent | None:
        if reading.goal_front is None and reading.fourth_corner is None:
            return None
        return MoistureContent(
            goal_front=reading.goal_front,
            fourth_corner=reading.fourth_corner,
            source=source,
            fetched_at=fetched_at,
        )

    return _fill_moisture(info, build(turf), build(dirt))


def _apply_going(
    info: TrackInfo,
    turf_going: str | None,
    dirt_going: str | None,
    source: str,
    fetched_at: datetime,
) -> TrackInfo:
    """Copy a printed going only when the course is still unknown."""
    turf = info.turf
    dirt = info.dirt
    if turf is not None and turf.condition == TrackCondition.UNKNOWN and turf_going:
        turf = turf.model_copy(
            update={
                "condition": TrackCondition.from_string(turf_going),
                "condition_source": source,
                "condition_fetched_at": fetched_at,
            }
        )
    if dirt is not None and dirt.condition == TrackCondition.UNKNOWN and dirt_going:
        dirt = dirt.model_copy(
            update={
                "condition": TrackCondition.from_string(dirt_going),
                "condition_source": source,
                "condition_fetched_at": fetched_at,
            }
        )
    if turf is info.turf and dirt is info.dirt:
        return info
    return info.model_copy(update={"turf": turf, "dirt": dirt})


def _fill_post_time(info: TrackInfo, text: str, source: str, fetched_at: datetime) -> TrackInfo:
    """Copy 発走 when this document prints one and the track info does not have it."""
    if info.post_time is not None or not text:
        return info
    clock = post_time_on_page(text)
    if clock is None:
        return info
    return info.model_copy(
        update={
            "post_time": clock,
            "post_time_source": source,
            "post_time_fetched_at": fetched_at,
        }
    )


def _json_post_time(payload: dict[str, object]) -> str:
    """Text passed to the post-time parser. A bare clock is marked as 発走."""
    raw = payload.get("post_time")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    text = raw.strip()
    if post_time_on_page(text):
        return text
    normalized = normalize_tokyo_clock(text)
    if normalized.endswith("+09:00"):
        return f"{normalized[:5]}発走"
    return ""
