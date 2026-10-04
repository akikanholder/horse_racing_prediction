"""Netkeiba track (baba) information scraper.

Source: https://race.netkeiba.com/race/track.html
"""

import logging
import re
from datetime import datetime
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

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
    TRACK_NOT_MEETING = "netkeiba track page is not the requested meeting"
    TRACK_UNPARSEABLE = "netkeiba track page could not be parsed"
    TRACK_FETCH_FAILED = "netkeiba track fetch failed"

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
        self, racecourse_id: str | None = None, date: str | None = None
    ) -> TrackInfo:
        """Scrape track information from Netkeiba.

        Args:
            racecourse_id: Racecourse ID (e.g., '06' for 中山)
            date: Date in YYYYMMDD format

        Returns:
            TrackInfo object with the scraped data
        """
        url = self.get_track_url(racecourse_id, date)
        try:
            html = self.http_client.get(url, encoding=self.ENCODING)
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            warning = self.TRACK_403 if status == 403 else self.TRACK_FETCH_FAILED
            logger.info("Netkeiba track fetch failed for %s (%s)", url, e)
            return empty_track_info("Netkeiba", warning)

        soup = BeautifulSoup(html, "lxml")
        requested = self.RACECOURSE_ID_MAPPING.get(racecourse_id) if racecourse_id else None
        if racecourse_id and requested is None:
            if racecourse_id in self.RACECOURSE_NAME_TO_ID:
                requested = racecourse_id
            else:
                return empty_track_info("Netkeiba", self.TRACK_UNPARSEABLE)
        page_venue = self._active_venue(soup)
        if requested:
            if page_venue != requested:
                warning = self.TRACK_NOT_MEETING if page_venue else self.TRACK_UNPARSEABLE
                return empty_track_info("Netkeiba", warning)
            return self._parse_track_info(soup, requested)
        if page_venue is None:
            return empty_track_info("Netkeiba", self.TRACK_UNPARSEABLE)
        return self._parse_track_info(soup, page_venue)

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

    def _parse_track_info(self, soup: BeautifulSoup, racecourse_name: str) -> TrackInfo:
        """Parse a page already matched to this meeting."""
        race_date = self._extract_race_date(soup)

        turf_info, turf_weather = self._extract_course_info(soup, CourseType.TURF)
        dirt_info, dirt_weather = self._extract_course_info(soup, CourseType.DIRT)

        weather = turf_weather or dirt_weather or Weather.UNKNOWN

        return TrackInfo(
            racecourse=racecourse_name,
            date=race_date,
            weather=weather,
            turf=turf_info,
            dirt=dirt_info,
            source="Netkeiba",
            warning=None,
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

    def _extract_race_date(self, soup: BeautifulSoup) -> datetime:
        """Extract the race date from the page."""
        date_patterns = [
            r"(\d{4})/(\d{1,2})/(\d{1,2})",
            r"(\d{4})年(\d{1,2})月(\d{1,2})日",
            r"(\d{1,2})月(\d{1,2})日",
            r"(\d{1,2})/(\d{1,2})",
        ]

        text = soup.get_text()
        for pattern in date_patterns:
            match = re.search(pattern, text)
            if match:
                groups = match.groups()
                if len(groups) == 3:
                    year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
                elif len(groups) == 2:
                    year = datetime.now().year
                    month, day = int(groups[0]), int(groups[1])
                else:
                    continue
                return datetime(year, month, day)

        return datetime.now()

    def _extract_course_info(
        self, soup: BeautifulSoup, course_type: CourseType
    ) -> tuple[CourseInfo | None, Weather | None]:
        """Extract course information for turf or dirt."""
        course_name = course_type.value

        sections = soup.find_all(["div", "section", "article"])
        target_section = None

        for section in sections:
            text = section.get_text()
            if course_name in text:
                if "天候" in text or "馬場" in text or "含水率" in text:
                    target_section = section
                    break

        if not target_section:
            text = soup.get_text()
            if course_name not in text:
                return None, None

        text = target_section.get_text() if target_section else soup.get_text()

        condition = self._extract_track_condition(text, course_type)
        weather = self._extract_weather(text)
        moisture = self._extract_moisture_content(text, target_section or soup)
        cushion = None
        if course_type == CourseType.TURF:
            cushion = self._extract_cushion_value(text, target_section or soup)

        course_info = CourseInfo(
            course_type=course_type,
            condition=condition,
            moisture=moisture,
            cushion=cushion,
        )

        return course_info, weather

    def _extract_track_condition(
        self, text: str, course_type: CourseType
    ) -> TrackCondition:
        """Extract track condition from text."""
        pattern = rf"馬場[：:]\s*([良稍重不]+)"
        match = re.search(pattern, text)
        if match:
            return TrackCondition.from_string(match.group(1))

        for cond in [
            TrackCondition.BAD,
            TrackCondition.HEAVY,
            TrackCondition.SLIGHTLY_HEAVY,
            TrackCondition.GOOD,
        ]:
            if cond.value in text:
                return cond

        return TrackCondition.UNKNOWN

    def _extract_weather(self, text: str) -> Weather:
        """Extract weather from text."""
        pattern = r"天候[：:]\s*([^\s]+)"
        match = re.search(pattern, text)
        if match:
            return Weather.from_string(match.group(1))

        for weather in Weather:
            if weather.value in text:
                return weather

        return Weather.UNKNOWN

    def _extract_cushion_value(self, text: str, soup) -> CushionValue | None:
        """Extract cushion value from text."""
        lines = text.split("\n")
        measured_at = None

        for i, line in enumerate(lines):
            if "クッション値" in line:
                for j in range(i + 1, min(i + 10, len(lines))):
                    current_line = lines[j].strip()
                    if not current_line:
                        continue
                    if re.match(r"^\d{4}[/\-]\d{1,2}[/\-]\d{1,2}", current_line):
                        time_match = re.search(
                            r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})\s+(\d{1,2}):(\d{1,2})",
                            current_line,
                        )
                        if time_match:
                            measured_at = datetime(
                                int(time_match.group(1)),
                                int(time_match.group(2)),
                                int(time_match.group(3)),
                                int(time_match.group(4)),
                                int(time_match.group(5)),
                            )
                        continue
                    match = re.match(r"^(\d+\.?\d*)$", current_line)
                    if match:
                        value = float(match.group(1))
                        if 3 <= value <= 20:
                            return CushionValue(value=value, measured_at=measured_at)
                break

        return None

    def _extract_moisture_content(
        self, text: str, soup
    ) -> MoistureContent | None:
        """Extract moisture content from text."""
        goal_front = None
        fourth_corner = None
        measured_at = None

        goal_pattern = r"ゴール前[：:\s]*(\d+\.?\d*)"
        match = re.search(goal_pattern, text)
        if match:
            goal_front = float(match.group(1))

        corner_pattern = r"4コーナー[：:\s]*(\d+\.?\d*)"
        match = re.search(corner_pattern, text)
        if match:
            fourth_corner = float(match.group(1))

        if goal_front is None and fourth_corner is None:
            lines = text.split("\n")
            for i, line in enumerate(lines):
                if "含水率" in line:
                    for j in range(i, min(i + 10, len(lines))):
                        if "ゴール前" in lines[j]:
                            match = re.search(r"(\d+\.?\d+)", lines[j])
                            if match:
                                goal_front = float(match.group(1))
                        if "4コーナー" in lines[j]:
                            match = re.search(r"(\d+\.?\d+)", lines[j])
                            if match:
                                fourth_corner = float(match.group(1))

        measured_at = self._extract_measurement_time(text, "含水率")

        if goal_front is not None or fourth_corner is not None:
            return MoistureContent(
                goal_front=goal_front,
                fourth_corner=fourth_corner,
                measured_at=measured_at,
            )

        return None

    def _extract_measurement_time(
        self, text: str, section_name: str
    ) -> datetime | None:
        """Extract measurement time for a specific section."""
        patterns = [
            rf"{section_name}[^\n]*?(\d{{4}})/(\d{{1,2}})/(\d{{1,2}})\s+(\d{{1,2}}):(\d{{1,2}})",
            rf"(\d{{4}})/(\d{{1,2}})/(\d{{1,2}})\s+(\d{{1,2}}):(\d{{1,2}})",
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.DOTALL)
            if match:
                groups = match.groups()
                if len(groups) >= 5:
                    return datetime(
                        int(groups[0]),
                        int(groups[1]),
                        int(groups[2]),
                        int(groups[3]),
                        int(groups[4]),
                    )

        return None

    def get_available_racecourses(self) -> list[tuple[str, str]]:
        """Get list of available racecourses.

        Returns:
            List of tuples (id, name)
        """
        return list(self.RACECOURSE_ID_MAPPING.items())
