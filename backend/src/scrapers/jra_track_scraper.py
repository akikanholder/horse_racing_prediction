"""JRA baba (track condition) scraper.

Source: https://www.jra.go.jp/keiba/baba/index.html
"""

import logging
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

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

    def scrape(self, racecourse: str | None = None) -> TrackInfo:
        """Scrape track information from the current JRA baba tabs.

        Direct venue URLs are not requested. A 403, a non-page, or a tab for
        a different meeting returns TrackInfo with null racecourse, cushion,
        and moisture plus ``warning``.

        Args:
            racecourse: Racecourse code (e.g., 'nakayama', 'tokyo') or a
                       Japanese name. If None, uses the first tab whose
                       title names a meeting.

        Returns:
            TrackInfo object with the scraped data, or an empty one
        """
        requested = self._requested_name(racecourse)
        if racecourse and requested is None:
            return empty_track_info("JRA", self.BABA_UNPARSEABLE)
        pages: list[str] = []
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
            pages.append(html)

        if requested:
            for html in pages:
                if self._title_venue(html) == requested:
                    soup = BeautifulSoup(html, "lxml")
                    return self._parse_track_info(soup, requested)
            return empty_track_info("JRA", self._miss_warning(pages, saw_403, saw_error))

        for html in pages:
            venue = self._title_venue(html)
            if venue:
                soup = BeautifulSoup(html, "lxml")
                return self._parse_track_info(soup, venue)
        return empty_track_info("JRA", self._miss_warning(pages, saw_403, saw_error))

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

    def _parse_track_info(self, soup: BeautifulSoup, racecourse_name: str) -> TrackInfo:
        """Parse track information from a page already matched to this meeting."""
        race_date = self._extract_race_date(soup)
        weather = self._extract_weather(soup)

        turf_condition = self._extract_track_condition(soup, CourseType.TURF)
        dirt_condition = self._extract_track_condition(soup, CourseType.DIRT)

        cushion_value = self._extract_cushion_value(soup)
        turf_moisture, dirt_moisture = self._extract_moisture_content(soup)

        turf_info = CourseInfo(
            course_type=CourseType.TURF,
            condition=turf_condition,
            moisture=turf_moisture,
            cushion=cushion_value,
        )

        dirt_info = CourseInfo(
            course_type=CourseType.DIRT,
            condition=dirt_condition,
            moisture=dirt_moisture,
        )

        grass_info = self._extract_grass_info(soup)
        weekly_info = self._extract_weekly_info(soup)

        return TrackInfo(
            racecourse=racecourse_name,
            date=race_date,
            weather=weather,
            turf=turf_info,
            dirt=dirt_info,
            grass_info=grass_info,
            weekly_info=weekly_info,
            source="JRA",
            warning=None,
        )

    def _extract_race_date(self, soup: BeautifulSoup) -> datetime:
        """Extract the race date from the page."""
        date_patterns = [
            r"(\d{4})年(\d{1,2})月(\d{1,2})日",
            r"(\d{1,2})月(\d{1,2})日",
        ]

        text = soup.get_text()
        for pattern in date_patterns:
            match = re.search(pattern, text)
            if match:
                groups = match.groups()
                if len(groups) == 3:
                    year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
                else:
                    year = datetime.now().year
                    month, day = int(groups[0]), int(groups[1])
                return datetime(year, month, day)

        return datetime.now()

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

    def _extract_cushion_value(self, soup: BeautifulSoup) -> CushionValue | None:
        """Extract cushion value for turf course."""
        text = soup.get_text()
        pattern = r"クッション値[^\d]*(\d+\.?\d*)"
        match = re.search(pattern, text)

        if match:
            value = float(match.group(1))
            measured_at = self._extract_measurement_time(soup, "クッション値")
            return CushionValue(value=value, measured_at=measured_at)

        return None

    def _extract_moisture_content(
        self, soup: BeautifulSoup
    ) -> tuple[MoistureContent | None, MoistureContent | None]:
        """Extract moisture content for turf and dirt courses."""
        turf_moisture = None
        dirt_moisture = None

        tables = soup.find_all("table")
        for table in tables:
            text = table.get_text()
            if "ゴール前" in text and "4コーナー" in text:
                rows = table.find_all("tr")
                for row in rows:
                    cells = row.find_all(["td", "th"])
                    if len(cells) >= 3:
                        label = cells[0].get_text().strip()
                        try:
                            goal_front = self._parse_float(cells[1].get_text())
                            fourth_corner = self._parse_float(cells[2].get_text())

                            moisture = MoistureContent(
                                goal_front=goal_front,
                                fourth_corner=fourth_corner,
                                measured_at=self._extract_measurement_time(
                                    soup, "含水率"
                                ),
                            )

                            if "芝" in label:
                                turf_moisture = moisture
                            elif "ダート" in label:
                                dirt_moisture = moisture
                        except (ValueError, IndexError):
                            continue

        return turf_moisture, dirt_moisture

    def _extract_measurement_time(
        self, soup: BeautifulSoup, section_name: str
    ) -> datetime | None:
        """Extract measurement time for a specific section."""
        text = soup.get_text()
        pattern = rf"{section_name}[^\n]*?(\d{{1,2}})月(\d{{1,2}})日[^\d]*(\d{{1,2}})[時:](\d{{1,2}})"
        match = re.search(pattern, text, re.DOTALL)

        if match:
            now = datetime.now()
            month, day, hour, minute = (
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                int(match.group(4)),
            )
            year = now.year
            if month > now.month + 1:
                year -= 1
            return datetime(year, month, day, hour, minute)

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
