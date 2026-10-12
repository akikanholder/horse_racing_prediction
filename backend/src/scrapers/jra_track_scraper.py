"""JRA baba (track condition) scraper.

Source: https://www.jra.go.jp/keiba/baba/index.html
"""

import logging
import re
from collections.abc import Callable
from datetime import datetime
from typing import Literal
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

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
from backend.scrapers.page_fallback import FetchedBody, PageFallback
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
    BABA_NOT_MEETING = "JRA baba page is not the requested meeting"
    BABA_UNPARSEABLE = "JRA baba page could not be parsed"
    BABA_FETCH_FAILED = "JRA baba fetch failed"
    BABA_LOGIN = "JRA baba requires login"
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
        self, racecourse: str | None = None, *, fetched_at: datetime | None = None
    ) -> TrackInfo:
        """Scrape track information from the current JRA baba tabs.

        Direct venue URLs are not requested. A 403, a non-page, or a tab for
        a different meeting returns TrackInfo with null racecourse, cushion,
        and moisture plus ``warning``. A 403 is not retried and is not evaded.

        When the meeting page loads but ``#cushion_num`` is empty, the same-site
        ``_data_cushion.html`` the page's own script calls is read next, then an
        optional headless render. The hardness scale is not the cushion value.

        Args:
            racecourse: Racecourse code (e.g., 'nakayama', 'tokyo') or a
                       Japanese name. If None, uses the first tab whose
                       title names a meeting.
            fetched_at: Timestamp stored on each copied value. Defaults to now
                       in Asia/Tokyo.

        Returns:
            TrackInfo object with the scraped data, or an empty one
        """
        fetched = fetched_at or tokyo_now()
        requested = self._requested_name(racecourse)
        if racecourse and requested is None:
            return empty_track_info("JRA", self.BABA_UNPARSEABLE)
        pages: list[tuple[str, str]] = []
        saw_403 = False
        saw_error = False
        for url in self.BABA_INDEX_URLS:
            try:
                html = self.http_client.get(url, encoding=self.BABA_ENCODING)
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status == 403:
                    saw_403 = True
                else:
                    saw_error = True
                logger.info("JRA baba fetch failed for %s (%s)", url, e)
                continue
            pages.append((url, html))

        chosen = self._choose_page(pages, requested)
        if chosen is None:
            htmls = [html for _, html in pages]
            return empty_track_info("JRA", self._miss_warning(htmls, saw_403, saw_error))
        url, html = chosen
        venue = self._title_venue(html) or requested
        if venue is None:
            return empty_track_info("JRA", self.BABA_UNPARSEABLE)
        loaded = self._enrich(url, html, fetched)
        if not loaded.pages:
            return empty_track_info("JRA", self._blocked_warning(loaded.warning))
        info = self._from_pages(loaded.pages, venue)
        if loaded.warning:
            info = info.model_copy(update={"warning": join_warnings(info.warning, loaded.warning)})
        return info

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

    def _miss_warning(self, pages: list[str], saw_403: bool, saw_error: bool) -> str:
        """Why no tab was copied. A foreign title is not this meeting."""
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

    def _blocked_warning(self, warning: str | None) -> str:
        """Map a stopped chain onto the baba warnings. Do not read the body."""
        if warning and "403" in warning:
            return self.BABA_403
        if warning and "login" in warning:
            return self.BABA_LOGIN
        return self.BABA_FETCH_FAILED

    def _enrich(self, url: str, html: str, fetched_at: datetime) -> object:
        """Follow the page's own cushion fragment when the number slot is empty."""
        fallback = PageFallback(
            self.http_client,
            site="jra",
            headless_render=self._headless_render,
            url_finder=self._baba_urls,
        )
        return fallback.load(
            url=url,
            params=None,
            headers=None,
            encoding=self.BABA_ENCODING,
            fetched_at=fetched_at,
            should_fallback=self._cushion_slot_empty,
            still_incomplete=self._still_missing_cushion,
            html_body=html,
        )

    def _baba_urls(self, html: str, page_url: str) -> list[str]:
        """``_data_cushion.html`` and ``_data_moist.html`` only."""

        def fetch(script_url: str) -> str:
            return fetch_or_block(
                self.http_client.get, script_url, encoding=self.BABA_ENCODING
            )

        return discover_jra_baba_urls(html, page_url, fetch)

    def _cushion_slot_empty(self, html: str) -> bool:
        """True when the page has a cushion slot and the number is not in it."""
        reading = cushion_from_html(BeautifulSoup(html, "lxml"))
        return reading.slot and reading.value is None

    def _still_missing_cushion(self, pages: list[FetchedBody]) -> bool:
        """True when none of the documents printed a cushion number."""
        venue = self._title_venue(pages[0].body) if pages else None
        if venue is None:
            return False
        info = self._from_pages(pages, venue)
        return _cushion_number(info) is None

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
        return info

    def _fill_from_page(self, info: TrackInfo, page: FetchedBody, venue: str) -> TrackInfo:
        """Copy a missing cushion, or moisture that the HTML did not print."""
        payload = json_object(page.body)
        if payload is not None:
            return self._fill_from_json(info, payload, page.source, page.fetched_at)
        soup = BeautifulSoup(page.body, "lxml")
        year = info.date.year if info.date is not None else printed_year(page.body)
        if _cushion_number(info) is None:
            cushion = self._cushion_from_fragment(soup, venue, year, page.source, page.fetched_at)
            if cushion is None:
                reading = cushion_from_html(soup)
                if reading.value is not None or reading.grade is not None:
                    cushion = CushionValue(
                        value=reading.value,
                        grade=reading.grade,
                        source=page.source,
                        fetched_at=page.fetched_at,
                    )
            if cushion is not None and info.turf is not None:
                info = info.model_copy(
                    update={"turf": info.turf.model_copy(update={"cushion": cushion})}
                )
        if _moisture_missing(info.turf) or _moisture_missing(info.dirt):
            turf_m, dirt_m = self._moisture_from_fragment(
                soup, venue, year, page.source, page.fetched_at
            )
            info = _fill_moisture(info, turf_m, dirt_m)
        return _fill_post_time(info, soup.get_text(" ", strip=True), page.source, page.fetched_at)

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
        turf_moisture, dirt_moisture = self._extract_moisture_content(soup, source, fetched_at)
        post = post_time_on_page(soup.get_text(" ", strip=True))

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
    ) -> tuple[MoistureContent | None, MoistureContent | None]:
        """Moisture printed in the goal / 4th-corner cells. The legend is ignored."""
        turf_moisture = None
        dirt_moisture = None
        measured_at = self._extract_measurement_time(soup, "含水率", None)

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
        """Latest moisture unit for this venue. Used only when the HTML cells are empty."""
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
        """Measurement clock printed with ``section_name``. No year means null."""
        text = soup.get_text()
        pattern = (
            rf"{section_name}[^\n]*?(\d{{1,2}})月(\d{{1,2}})日[^\d]*(\d{{1,2}})[時:](\d{{1,2}})"
        )
        match = re.search(pattern, text, re.DOTALL)
        if match is None or year is None:
            return None
        return datetime(
            year,
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
        )

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
                                rainfall = self._parse_float(
                                    rainfall_row[i].get_text()
                                )

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


def _moisture_missing(course: CourseInfo | None) -> bool:
    """True when this course has no printed moisture number."""
    if course is None or course.moisture is None:
        return True
    return course.moisture.goal_front is None and course.moisture.fourth_corner is None


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


def _fill_moisture(
    info: TrackInfo,
    turf: MoistureContent | None,
    dirt: MoistureContent | None,
) -> TrackInfo:
    """Fill moisture only where the HTML did not already print it."""
    turf_update = info.turf
    dirt_update = info.dirt
    if turf is not None and _moisture_missing(info.turf) and info.turf is not None:
        turf_update = info.turf.model_copy(update={"moisture": turf})
    if dirt is not None and _moisture_missing(info.dirt) and info.dirt is not None:
        dirt_update = info.dirt.model_copy(update={"moisture": dirt})
    if turf_update is info.turf and dirt_update is info.dirt:
        return info
    return info.model_copy(update={"turf": turf_update, "dirt": dirt_update})


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


def _fill_post_time(
    info: TrackInfo, text: str, source: str, fetched_at: datetime
) -> TrackInfo:
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
