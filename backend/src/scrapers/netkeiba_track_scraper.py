"""Netkeiba track (baba) information scraper.

Source: https://race.netkeiba.com/race/track.html
"""

import logging
import re
from collections.abc import Callable
from datetime import date, datetime
from typing import Literal
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup, Tag

from backend.models.track import (
    CourseInfo,
    CourseType,
    CushionValue,
    MoistureContent,
    TrackCondition,
    TrackInfo,
    Weather,
    empty_track_info,
)
from backend.scrapers.base_scraper import BaseScraper
from backend.scrapers.page_fallback import FetchedBody, PageFallback
from backend.scrapers.track_freshness import (
    CACHED_COPY,
    FRESH_REQUEST_HEADERS,
    apply_measurement_freshness,
    cached_copy_seen,
    clear_cached_copy_flag,
    page_measurement_time,
)
from backend.scrapers.track_readings import (
    CUSHION_MISSING,
    PrintedMoisture,
    cushion_from_html,
    cushion_from_json,
    going_label,
    join_warnings,
    json_object,
    moisture_pair,
    post_time_on_page,
    printed_float,
    printed_year,
    weather_label,
)
from backend.utils.date_utils import parse_measurement_time, tokyo_now

logger = logging.getLogger(__name__)


class NetkeibaTrackScraper(BaseScraper):
    """Scraper for Netkeiba track (baba) information."""

    BASE_URL = "https://race.netkeiba.com"
    TRACK_INFO_PATH = "/race/track.html"

    RACECOURSE_ID_MAPPING = {
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

    RACECOURSE_NAME_TO_ID = {v: k for k, v in RACECOURSE_ID_MAPPING.items()}
    TRACK_403 = "netkeiba track returned 403"
    TRACK_LOGIN = "netkeiba track requires login"
    TRACK_NOT_MEETING = "netkeiba track page is not the requested meeting"
    TRACK_UNPARSEABLE = "netkeiba track page could not be parsed"
    TRACK_FETCH_FAILED = "netkeiba track fetch failed"
    CUSHION_MISSING = CUSHION_MISSING

    def __init__(
        self,
        http_client: object | None = None,
        *,
        headless_render: Callable[[str], str] | Literal["auto", "off"] = "auto",
    ):
        """Create a scraper.

        ``headless_render="off"`` skips the browser step and does not warn.
        ``"auto"`` uses Playwright when the browser is installed.
        """
        super().__init__(http_client)  # type: ignore[arg-type]
        self._headless_render = headless_render
        self._requested_venue: str | None = None

    def get_base_url(self) -> str:
        """Return the base URL for Netkeiba."""
        return self.BASE_URL

    def get_track_url(
        self, racecourse_id: str | None = None, date: str | None = None
    ) -> str:
        """Get the URL for track information.

        Args:
            racecourse_id: Racecourse ID (e.g., '06' for 中山)
            date: Date in YYYYMMDD format

        Returns:
            URL for the track information page
        """
        base = urljoin(self.BASE_URL, self.TRACK_INFO_PATH)
        params = {}

        if racecourse_id:
            params["jyo"] = racecourse_id
        if date:
            params["kaisai_date"] = date

        if params:
            return f"{base}?{urlencode(params)}"
        return base

    def scrape(
        self,
        racecourse_id: str | None = None,
        date: str | None = None,
        *,
        fetched_at: datetime | None = None,
        race_date: date | None = None,
        today: date | None = None,
        race_date_warning: str | None = None,
    ) -> TrackInfo:
        """Scrape track information from Netkeiba.

        The public page is read as HTML, then JSON embedded in that page or a
        same-site URL the page calls, then an optional headless render. HTTP
        403 and a login wall stop that chain. The hardness scale is not copied.

        Args:
            racecourse_id: Racecourse ID (e.g., '06' for 中山)
            date: Date in YYYYMMDD format
            fetched_at: Timestamp stored on each copied value. Defaults to now
                       in Asia/Tokyo.
            race_date: Day the reading is for. Null means today in Asia/Tokyo.
            today: Clock date used for ``is_today``. Defaults to today.
            race_date_warning: Set when the race date could not be read.

        Returns:
            TrackInfo object with the scraped data
        """
        fetched = fetched_at or tokyo_now()
        clear_cached_copy_flag(self.http_client)
        requested = self._requested_venue_name(racecourse_id)
        if racecourse_id and requested is None:
            return self._finish(
                empty_track_info("Netkeiba", self.TRACK_UNPARSEABLE),
                race_date=race_date,
                today=today,
                race_date_warning=race_date_warning,
            )
        self._requested_venue = requested
        url = self.get_track_url(
            self.RACECOURSE_NAME_TO_ID.get(requested, racecourse_id) if requested else racecourse_id,
            date,
        )
        fallback = PageFallback(
            self.http_client,
            site="netkeiba",
            headless_render=self._headless_render,
        )
        loaded = fallback.load(
            url=url,
            params=None,
            headers=dict(FRESH_REQUEST_HEADERS),
            encoding=self.ENCODING,
            fetched_at=fetched,
            should_fallback=self._needs_fallback,
            still_incomplete=self._still_missing_cushion,
        )
        if not loaded.pages:
            return self._finish(
                empty_track_info("Netkeiba", self._blocked_warning(loaded.warning)),
                race_date=race_date,
                today=today,
                race_date_warning=race_date_warning,
            )

        soup = BeautifulSoup(loaded.pages[0].body, "lxml")
        page_venue = self._active_venue(soup)
        if requested:
            if page_venue != requested:
                warning = self.TRACK_NOT_MEETING if page_venue else self.TRACK_UNPARSEABLE
                return self._finish(
                    empty_track_info("Netkeiba", warning),
                    race_date=race_date,
                    today=today,
                    race_date_warning=race_date_warning,
                )
            venue = requested
        elif page_venue is None:
            return self._finish(
                empty_track_info("Netkeiba", self.TRACK_UNPARSEABLE),
                race_date=race_date,
                today=today,
                race_date_warning=race_date_warning,
            )
        else:
            venue = page_venue

        info = self._from_pages(loaded.pages, venue)
        if loaded.warning:
            info = info.model_copy(update={"warning": join_warnings(info.warning, loaded.warning)})
        if loaded.status == "blocked":
            info = info.model_copy(
                update={"warning": join_warnings(info.warning, self._blocked_warning(loaded.warning))}
            )
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
        race_date_warning: str | None,
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

    def scrape_by_name(
        self, racecourse_name: str, date: str | None = None
    ) -> TrackInfo:
        """Scrape track information by racecourse name.

        Args:
            racecourse_name: Racecourse name in Japanese (e.g., '中山')
            date: Date in YYYYMMDD format

        Returns:
            TrackInfo object with the scraped data

        Raises:
            ValueError: If racecourse name is not recognized
        """
        racecourse_id = self.RACECOURSE_NAME_TO_ID.get(racecourse_name)
        if not racecourse_id:
            raise ValueError(f"Unknown racecourse: {racecourse_name}")
        return self.scrape(racecourse_id, date)

    def _requested_venue_name(self, racecourse_id: str | None) -> str | None:
        """Japanese meeting name. An unknown id stays unset."""
        if not racecourse_id:
            return None
        if racecourse_id in self.RACECOURSE_ID_MAPPING:
            return self.RACECOURSE_ID_MAPPING[racecourse_id]
        if racecourse_id in self.RACECOURSE_NAME_TO_ID:
            return racecourse_id
        return None

    def _blocked_warning(self, warning: str | None) -> str:
        """Map a stopped chain onto the track warnings. Do not read the body."""
        if warning and "403" in warning:
            return self.TRACK_403
        if warning and "login" in warning:
            return self.TRACK_LOGIN
        return self.TRACK_FETCH_FAILED

    def _needs_fallback(self, html: str) -> bool:
        """True when this meeting's cushion slot is empty. A foreign tab is not read."""
        soup = BeautifulSoup(html, "lxml")
        venue = self._active_venue(soup)
        if venue is None or (self._requested_venue and venue != self._requested_venue):
            return False
        reading = cushion_from_html(self._scope(soup, venue))
        return reading.slot and reading.value is None

    def _still_missing_cushion(self, pages: list[FetchedBody]) -> bool:
        """True when the documents still have no cushion number."""
        if not pages:
            return False
        soup = BeautifulSoup(pages[0].body, "lxml")
        venue = self._active_venue(soup) or self._requested_venue
        if venue is None:
            return False
        info = self._from_pages(pages, venue)
        return _cushion_number(info) is None

    def _from_pages(self, pages: list[FetchedBody], venue: str) -> TrackInfo:
        """Merge HTML with later JSON or a headless render."""
        first = pages[0]
        info = self._parse_track_info(
            BeautifulSoup(first.body, "lxml"),
            venue,
            source=first.source,
            fetched_at=first.fetched_at,
        )
        for page in pages[1:]:
            info = self._fill_from_page(info, page)
        if _cushion_number(info) is None:
            info = info.model_copy(
                update={"warning": join_warnings(info.warning, self.CUSHION_MISSING)}
            )
        return info

    def _fill_from_page(self, info: TrackInfo, page: FetchedBody) -> TrackInfo:
        """Copy a missing cushion. Moisture already printed on the HTML stays."""
        payload = json_object(page.body)
        if payload is not None:
            return self._fill_from_json(info, payload, page.source, page.fetched_at)
        soup = BeautifulSoup(page.body, "lxml")
        extra = self._parse_track_info(
            soup, info.racecourse or "", source=page.source, fetched_at=page.fetched_at
        )
        return _merge_missing(info, extra)

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
        info = _fill_moisture_readings(
            info,
            moisture_pair(payload, "goal_front", "fourth_corner"),
            moisture_pair(payload, "dirt_goal_front", "dirt_fourth_corner"),
            source,
            fetched_at,
        )
        info = _fill_going(
            info,
            going_label(payload, "going") or going_label(payload, "turf_going"),
            going_label(payload, "dirt_going"),
            source,
            fetched_at,
        )
        label = weather_label(payload)
        if label and info.weather == Weather.UNKNOWN:
            parsed = Weather.from_string(label)
            if parsed != Weather.UNKNOWN:
                info = info.model_copy(
                    update={
                        "weather": parsed,
                        "weather_source": source,
                        "weather_fetched_at": fetched_at,
                    }
                )
        raw_post = payload.get("post_time")
        if info.post_time is None and isinstance(raw_post, str):
            clock = post_time_on_page(raw_post) or post_time_on_page(f"{raw_post.strip()}発走")
            if clock:
                info = info.model_copy(
                    update={
                        "post_time": clock,
                        "post_time_source": source,
                        "post_time_fetched_at": fetched_at,
                    }
                )
        return info

    def _scope(self, soup: BeautifulSoup, venue: str | None) -> BeautifulSoup | Tag:
        """The active venue panel. Other meetings on the page are left out."""
        if venue is None:
            return soup
        code = self.RACECOURSE_NAME_TO_ID.get(venue)
        if code is None:
            return soup
        panel = soup.select_one(f".TrackTabArea00.Jyo_{code}")
        if isinstance(panel, Tag):
            return panel
        return soup

    def _parse_track_info(
        self,
        soup: BeautifulSoup,
        racecourse_name: str,
        *,
        source: str,
        fetched_at: datetime,
    ) -> TrackInfo:
        """Parse a page already matched to this meeting."""
        scoped = self._scope(soup, racecourse_name)
        race_date = self._extract_race_date(soup)
        turf_root = scoped.select_one(".TrackTurf01") if isinstance(scoped, Tag) else None
        dirt_root = scoped.select_one(".TrackDirt01") if isinstance(scoped, Tag) else None
        turf_info, turf_weather = self._extract_course_info(
            turf_root if isinstance(turf_root, Tag) else scoped,
            CourseType.TURF,
            source,
            fetched_at,
        )
        dirt_info, dirt_weather = self._extract_course_info(
            dirt_root if isinstance(dirt_root, Tag) else scoped,
            CourseType.DIRT,
            source,
            fetched_at,
        )
        weather = turf_weather or dirt_weather or Weather.UNKNOWN
        text = scoped.get_text(" ", strip=True) if isinstance(scoped, Tag) else soup.get_text()
        post = post_time_on_page(text)
        year = printed_year(soup.get_text())
        stamp = page_measurement_time(soup.get_text("\n", strip=True), year)
        return TrackInfo(
            racecourse=racecourse_name,
            date=race_date,
            weather=weather,
            weather_source=source if weather != Weather.UNKNOWN else None,
            weather_fetched_at=fetched_at if weather != Weather.UNKNOWN else None,
            turf=turf_info,
            dirt=dirt_info,
            source="Netkeiba",
            measured_at=stamp,
            warning=None,
            post_time=post,
            post_time_source=source if post else None,
            post_time_fetched_at=fetched_at if post else None,
        )

    def _active_venue(self, soup: BeautifulSoup) -> str | None:
        """Venue of the selected tab. Other links on the page are ignored."""
        names = set(self.RACECOURSE_ID_MAPPING.values())
        found: list[str] = []
        for tab in soup.find_all(["li", "a", "span"]):
            classes = tab.get("class") or []
            if not any(str(name).lower() in {"active", "current", "selected"} for name in classes):
                continue
            text = tab.get_text(strip=True)
            if text in names and text not in found:
                found.append(text)
        if len(found) == 1:
            return found[0]
        return None

    def _extract_race_date(self, soup: BeautifulSoup) -> datetime | None:
        """Date when the page prints a year. A month and day alone stay null."""
        text = soup.get_text()
        for pattern in (r"(\d{4})/(\d{1,2})/(\d{1,2})", r"(\d{4})年(\d{1,2})月(\d{1,2})日"):
            match = re.search(pattern, text)
            if match:
                return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return None

    def _extract_course_info(
        self,
        root: BeautifulSoup | Tag,
        course_type: CourseType,
        source: str,
        fetched_at: datetime,
    ) -> tuple[CourseInfo | None, Weather | None]:
        """Read one course. The hardness scale and the going legend are not values."""
        classes = [str(name) for name in (root.get("class") or [])] if isinstance(root, Tag) else []
        named = "TrackTurf01" in classes or "TrackDirt01" in classes
        text = root.get_text("\n", strip=True)
        if not named and course_type.value not in text:
            return None, None
        condition = self._extract_track_condition(text)
        weather = self._extract_weather(text)
        moisture = self._extract_moisture_content(root, source, fetched_at)
        cushion = None
        if course_type == CourseType.TURF:
            cushion = self._extract_cushion_value(root, source, fetched_at)
        if (
            cushion is None
            and moisture is None
            and condition == TrackCondition.UNKNOWN
            and weather == Weather.UNKNOWN
            and not named
        ):
            return None, None
        return (
            CourseInfo(
                course_type=course_type,
                condition=condition,
                condition_source=source if condition != TrackCondition.UNKNOWN else None,
                condition_fetched_at=fetched_at if condition != TrackCondition.UNKNOWN else None,
                moisture=moisture,
                cushion=cushion,
            ),
            weather,
        )

    def _extract_track_condition(self, text: str) -> TrackCondition:
        """Going printed as ``馬場：``. The moisture legend is not a going."""
        match = re.search(r"馬場[：:]\s*([良稍重不]+)", text)
        if match is None:
            return TrackCondition.UNKNOWN
        return TrackCondition.from_string(match.group(1))

    def _extract_weather(self, text: str) -> Weather:
        """Weather printed as ``天候：``."""
        match = re.search(r"天候[：:]\s*(\S+)", text)
        if match is None:
            return Weather.UNKNOWN
        return Weather.from_string(match.group(1))

    def _extract_cushion_value(
        self, root: BeautifulSoup | Tag, source: str, fetched_at: datetime
    ) -> CushionValue | None:
        """Number in the cushion slot. Bar ticks 12/10/8/7 are not the reading."""
        reading = cushion_from_html(root)
        if reading.value is None and reading.grade is None:
            return None
        measured = None
        if isinstance(root, Tag):
            box = root.select_one(".RaceTrackBox01.Cushion") or root
            stamp = box.select_one(".UpDataTime")
            if isinstance(stamp, Tag):
                measured = parse_measurement_time(
                    stamp.get_text(" ", strip=True), printed_year(root.get_text())
                )
        return CushionValue(
            value=reading.value,
            grade=reading.grade,
            measured_at=measured,
            source=source,
            fetched_at=fetched_at,
        )

    def _extract_moisture_content(
        self, root: BeautifulSoup | Tag, source: str, fetched_at: datetime
    ) -> MoistureContent | None:
        """Goal and corner numbers printed next to those labels."""
        goal_front, fourth_corner = _moisture_numbers(root)
        if goal_front is None and fourth_corner is None:
            return None
        measured = None
        if isinstance(root, Tag):
            stamp = root.select_one(".Moisture .UpDataTime") or root.select_one(".UpDataTime")
            if isinstance(stamp, Tag):
                measured = parse_measurement_time(
                    stamp.get_text(" ", strip=True), printed_year(root.get_text())
                )
        return MoistureContent(
            goal_front=goal_front,
            fourth_corner=fourth_corner,
            measured_at=measured,
            source=source,
            fetched_at=fetched_at,
        )

    def get_available_racecourses(self) -> list[tuple[str, str]]:
        """Get list of available racecourses.

        Returns:
            List of tuples (id, name)
        """
        return list(self.RACECOURSE_ID_MAPPING.items())


def _cushion_number(info: TrackInfo) -> float | None:
    """Printed cushion number, or null when the slot is empty."""
    if info.turf is None or info.turf.cushion is None:
        return None
    return info.turf.cushion.value


def _moisture_missing(course: CourseInfo | None) -> bool:
    """True when this course has no printed moisture number."""
    if course is None or course.moisture is None:
        return True
    return course.moisture.goal_front is None and course.moisture.fourth_corner is None


def _moisture_numbers(root: BeautifulSoup | Tag) -> tuple[float | None, float | None]:
    """Goal and corner as printed. A bare number in the legend is not moisture."""
    goal_front = None
    fourth_corner = None
    if isinstance(root, Tag):
        wraps = root.select(".MoistureWrap01") or []
        search = wraps or [root]
        for wrap in search:
            for node in wrap.find_all("dl"):
                if not isinstance(node, Tag):
                    continue
                label = node.get_text(" ", strip=True)
                cell = node.find("dd")
                if not isinstance(cell, Tag):
                    continue
                number = printed_float(cell.get_text())
                if number is None:
                    continue
                if "ゴール前" in label:
                    goal_front = number
                elif "4コーナー" in label:
                    fourth_corner = number
    if goal_front is None and fourth_corner is None:
        text = root.get_text("\n", strip=True)
        goal_front = _labeled_float(text, "ゴール前")
        fourth_corner = _labeled_float(text, "4コーナー")
    return goal_front, fourth_corner


def _labeled_float(text: str, label: str) -> float | None:
    """Number on the label's line, or the next line when that line is only a number."""
    lines = [line.strip() for line in text.split("\n")]
    for index, line in enumerate(lines):
        if label not in line:
            continue
        same = printed_float(line.replace(label, " ").replace("：", " ").replace(":", " "))
        if same is not None:
            return same
        if index + 1 < len(lines):
            nxt = printed_float(lines[index + 1])
            if nxt is not None:
                return nxt
    return None


def _fill_moisture_readings(
    info: TrackInfo,
    turf: PrintedMoisture,
    dirt: PrintedMoisture,
    source: str,
    fetched_at: datetime,
) -> TrackInfo:
    """Copy JSON moisture only where the HTML printed none."""

    def build(reading: PrintedMoisture) -> MoistureContent | None:
        if reading.goal_front is None and reading.fourth_corner is None:
            return None
        return MoistureContent(
            goal_front=reading.goal_front,
            fourth_corner=reading.fourth_corner,
            source=source,
            fetched_at=fetched_at,
        )

    turf_course = info.turf
    dirt_course = info.dirt
    turf_m = build(turf)
    dirt_m = build(dirt)
    if turf_m is not None and _moisture_missing(turf_course) and turf_course is not None:
        turf_course = turf_course.model_copy(update={"moisture": turf_m})
    if dirt_m is not None and _moisture_missing(dirt_course) and dirt_course is not None:
        dirt_course = dirt_course.model_copy(update={"moisture": dirt_m})
    if turf_course is info.turf and dirt_course is info.dirt:
        return info
    return info.model_copy(update={"turf": turf_course, "dirt": dirt_course})


def _fill_going(
    info: TrackInfo,
    turf_going: str | None,
    dirt_going: str | None,
    source: str,
    fetched_at: datetime,
) -> TrackInfo:
    """Copy a printed going only when that course is still unknown."""
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


def _merge_missing(primary: TrackInfo, extra: TrackInfo) -> TrackInfo:
    """Fill holes from a later document. Numbers already printed stay."""
    turf = _merge_course(primary.turf, extra.turf)
    dirt = _merge_course(primary.dirt, extra.dirt)
    weather = primary.weather
    weather_source = primary.weather_source
    weather_fetched = primary.weather_fetched_at
    if weather == Weather.UNKNOWN and extra.weather != Weather.UNKNOWN:
        weather = extra.weather
        weather_source = extra.weather_source
        weather_fetched = extra.weather_fetched_at
    post_time = primary.post_time or extra.post_time
    post_source = primary.post_time_source if primary.post_time else extra.post_time_source
    post_fetched = primary.post_time_fetched_at if primary.post_time else extra.post_time_fetched_at
    return primary.model_copy(
        update={
            "weather": weather,
            "weather_source": weather_source,
            "weather_fetched_at": weather_fetched,
            "turf": turf,
            "dirt": dirt,
            "post_time": post_time,
            "post_time_source": post_source,
            "post_time_fetched_at": post_fetched,
            "date": primary.date or extra.date,
        }
    )


def _merge_course(primary: CourseInfo | None, extra: CourseInfo | None) -> CourseInfo | None:
    """Prefer readings already copied from the first page."""
    if primary is None:
        return extra
    if extra is None:
        return primary
    updates: dict[str, object] = {}
    if (primary.cushion is None or primary.cushion.value is None) and extra.cushion is not None:
        if extra.cushion.value is not None or extra.cushion.grade is not None:
            updates["cushion"] = extra.cushion
    if _moisture_missing(primary) and extra.moisture is not None:
        updates["moisture"] = extra.moisture
    if primary.condition == TrackCondition.UNKNOWN and extra.condition != TrackCondition.UNKNOWN:
        updates["condition"] = extra.condition
        updates["condition_source"] = extra.condition_source
        updates["condition_fetched_at"] = extra.condition_fetched_at
    if not updates:
        return primary
    return primary.model_copy(update=updates)
