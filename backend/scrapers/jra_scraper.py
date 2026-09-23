"""JRA official website scraper."""

import logging
import re
from datetime import date, datetime

from bs4 import Tag

from backend.models.course import Course, CourseCondition, WeeklyVenueSchedule
from backend.models.race import Race, RaceSchedule
from backend.scrapers.base_scraper import BaseScraper
from backend.utils.date_utils import get_current_week_dates, parse_japanese_date

logger = logging.getLogger(__name__)


class JRAScraper(BaseScraper):
    """Scraper for JRA official website (https://jra.jp)."""

    BASE_URL = "https://jra.jp"
    ENCODING = "utf-8"

    VENUE_CODES = {
        "札幌": "01",
        "函館": "02",
        "福島": "03",
        "新潟": "04",
        "東京": "05",
        "中山": "06",
        "中京": "07",
        "京都": "08",
        "阪神": "09",
        "小倉": "10",
    }

    def get_weekly_race_schedule(self) -> RaceSchedule:
        """Get this week's race schedule from JRA.

        Returns:
            RaceSchedule containing this week's races
        """
        week_start, week_end = get_current_week_dates()
        races: list[Race] = []

        soup = self.fetch_page(f"{self.BASE_URL}/keiba/")

        schedule_section = soup.find("div", class_="schedule") or soup.find(
            "section", id="schedule"
        )

        if schedule_section:
            races.extend(self._parse_schedule_section(schedule_section, week_start))

        race_list = soup.find("div", class_="race_list") or soup.find(
            "ul", class_="race_list"
        )
        if race_list:
            races.extend(self._parse_race_list(race_list, week_start))

        main_content = soup.find("main") or soup.find("div", id="main")
        if main_content and not races:
            races.extend(self._extract_races_from_content(main_content, week_start))

        return RaceSchedule(
            week_start=week_start,
            week_end=week_end,
            races=races,
            fetched_at=datetime.now(),
        )

    def _parse_schedule_section(
        self, section: Tag, default_date: date
    ) -> list[Race]:
        """Parse schedule section for race information."""
        races = []

        date_blocks = section.find_all(["div", "section"], class_=re.compile(r"day|date"))
        for block in date_blocks:
            race_date = self._extract_date_from_block(block) or default_date
            venue_name = self._extract_venue_from_block(block)

            race_items = block.find_all(["li", "a"], class_=re.compile(r"race"))
            for item in race_items:
                race = self._parse_race_item(item, race_date, venue_name)
                if race:
                    races.append(race)

        return races

    def _parse_race_list(self, race_list: Tag, default_date: date) -> list[Race]:
        """Parse race list element."""
        races = []

        items = race_list.find_all("li")
        for item in items:
            race = self._parse_race_item(item, default_date, None)
            if race:
                races.append(race)

        return races

    def _extract_races_from_content(
        self, content: Tag, default_date: date
    ) -> list[Race]:
        """Extract race information from main content area."""
        races = []

        links = content.find_all("a", href=re.compile(r"race|shutuba|result"))
        for link in links:
            href = link.get("href", "")
            text = self.clean_text(link.get_text())

            race_info = self._parse_race_link(href, text, default_date)
            if race_info:
                races.append(race_info)

        return races

    def _parse_race_item(
        self, item: Tag, race_date: date, default_venue: str | None
    ) -> Race | None:
        """Parse individual race item."""
        try:
            link = item.find("a") if item.name != "a" else item
            if not link:
                return None

            href = link.get("href", "")
            text = self.clean_text(link.get_text())

            race_number = self._extract_race_number(text) or self._extract_race_number(href)
            if not race_number:
                return None

            race_name = self._extract_race_name(text)
            venue = self._extract_venue_name(text) or default_venue or "不明"
            distance, track_type = self._extract_distance_info(text)

            race_id = self._generate_race_id(race_date, venue, race_number)

            return Race(
                race_id=race_id,
                race_name=race_name or f"{race_number}R",
                race_number=race_number,
                race_date=race_date,
                venue=venue,
                venue_code=self.VENUE_CODES.get(venue),
                distance=distance or 0,
                track_type=track_type or "不明",
            )

        except Exception as e:
            logger.warning(f"Failed to parse race item: {e}")
            return None

    def _parse_race_link(
        self, href: str, text: str, default_date: date
    ) -> Race | None:
        """Parse race from link."""
        race_number = self._extract_race_number(text) or self._extract_race_number(href)
        if not race_number:
            return None

        race_name = self._extract_race_name(text)
        venue = self._extract_venue_name(text) or "不明"
        distance, track_type = self._extract_distance_info(text)

        race_id = self._generate_race_id(default_date, venue, race_number)

        return Race(
            race_id=race_id,
            race_name=race_name or f"{race_number}R",
            race_number=race_number,
            race_date=default_date,
            venue=venue,
            venue_code=self.VENUE_CODES.get(venue),
            distance=distance or 0,
            track_type=track_type or "不明",
        )

    def _extract_date_from_block(self, block: Tag) -> date | None:
        """Extract date from a block element."""
        text = self.clean_text(block.get_text())
        return parse_japanese_date(text)

    def _extract_venue_from_block(self, block: Tag) -> str | None:
        """Extract venue name from block."""
        text = self.clean_text(block.get_text())
        for venue in self.VENUE_CODES.keys():
            if venue in text:
                return venue
        return None

    def _extract_race_number(self, text: str) -> int | None:
        """Extract race number from text."""
        match = re.search(r"(\d{1,2})\s*[Rr]", text)
        if match:
            return int(match.group(1))
        return None

    def _extract_race_name(self, text: str) -> str | None:
        """Extract race name from text."""
        patterns = [
            r"(\d{1,2}R\s*)(.+?)(?:\s*芝|\s*ダ|\s*\d+m|$)",
            r"(\d{1,2}R\s*)(.+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                name = match.group(2).strip()
                if name and name not in ["芝", "ダート", "ダ"]:
                    return name
        return None

    def _extract_venue_name(self, text: str) -> str | None:
        """Extract venue name from text."""
        for venue in self.VENUE_CODES.keys():
            if venue in text:
                return venue
        return None

    def _extract_distance_info(self, text: str) -> tuple[int | None, str | None]:
        """Extract distance and track type from text."""
        distance = None
        track_type = None

        distance_match = re.search(r"(\d{3,4})\s*m", text)
        if distance_match:
            distance = int(distance_match.group(1))

        if "芝" in text:
            track_type = "芝"
        elif "ダート" in text or "ダ" in text:
            track_type = "ダート"
        elif "障" in text:
            track_type = "障害"

        return distance, track_type

    def _generate_race_id(
        self, race_date: date, venue: str, race_number: int
    ) -> str:
        """Generate race ID."""
        venue_code = self.VENUE_CODES.get(venue, "00")
        return f"{race_date.strftime('%Y%m%d')}{venue_code}{race_number:02d}"

    def get_venue_schedules(self) -> list[WeeklyVenueSchedule]:
        """Get weekly venue schedules.

        Returns:
            List of venue schedules for this week
        """
        schedules: list[WeeklyVenueSchedule] = []
        week_start, week_end = get_current_week_dates()

        soup = self.fetch_page(f"{self.BASE_URL}/keiba/")

        for venue_name, venue_code in self.VENUE_CODES.items():
            text = soup.get_text()
            if venue_name in text:
                schedule = WeeklyVenueSchedule(
                    venue=venue_name,
                    venue_code=venue_code,
                    dates=[week_start, week_end],
                    fetched_at=datetime.now(),
                )
                schedules.append(schedule)

        return schedules

    def get_course_conditions(self, venue: str) -> CourseCondition | None:
        """Get current course conditions for a venue.

        Args:
            venue: Venue name (e.g., "東京")

        Returns:
            CourseCondition or None if not available
        """
        try:
            soup = self.fetch_page(f"{self.BASE_URL}/keiba/baba/")

            venue_section = soup.find(string=re.compile(venue))
            if not venue_section:
                return None

            parent = venue_section.find_parent(["div", "section", "table"])
            if not parent:
                return None

            text = self.clean_text(parent.get_text())

            turf_condition = None
            dirt_condition = None

            if "芝" in text:
                for condition in ["良", "稍重", "重", "不良"]:
                    if f"芝{condition}" in text or f"芝 {condition}" in text:
                        turf_condition = condition
                        break
                    pattern = rf"芝[：:]\s*{condition}"
                    if re.search(pattern, text):
                        turf_condition = condition
                        break

            if "ダート" in text or "ダ" in text:
                for condition in ["良", "稍重", "重", "不良"]:
                    if f"ダート{condition}" in text or f"ダ{condition}" in text:
                        dirt_condition = condition
                        break

            return CourseCondition(
                venue=venue,
                condition_date=date.today(),
                turf_condition=turf_condition,
                dirt_condition=dirt_condition,
            )

        except Exception as e:
            logger.warning(f"Failed to get course conditions for {venue}: {e}")
            return None

    def get_course_info(self, venue: str) -> Course | None:
        """Get course information for a venue.

        Args:
            venue: Venue name

        Returns:
            Course information
        """
        venue_code = self.VENUE_CODES.get(venue)
        if not venue_code:
            return None

        course_data = {
            "東京": {
                "turf_circumference": 2083,
                "dirt_circumference": 1899,
                "turf_straight": 525,
                "dirt_straight": 501,
            },
            "中山": {
                "turf_circumference": 1840,
                "dirt_circumference": 1493,
                "turf_straight": 310,
                "dirt_straight": 308,
            },
            "阪神": {
                "turf_circumference": 2089,
                "dirt_circumference": 1517,
                "turf_straight": 473,
                "dirt_straight": 352,
            },
            "京都": {
                "turf_circumference": 1894,
                "dirt_circumference": 1608,
                "turf_straight": 403,
                "dirt_straight": 329,
            },
            "中京": {
                "turf_circumference": 1705,
                "dirt_circumference": 1530,
                "turf_straight": 412,
                "dirt_straight": 411,
            },
            "小倉": {
                "turf_circumference": 1615,
                "dirt_circumference": 1445,
                "turf_straight": 293,
                "dirt_straight": 291,
            },
            "新潟": {
                "turf_circumference": 2223,
                "dirt_circumference": 1472,
                "turf_straight": 659,
                "dirt_straight": 354,
            },
            "福島": {
                "turf_circumference": 1600,
                "dirt_circumference": 1444,
                "turf_straight": 292,
                "dirt_straight": 296,
            },
            "札幌": {
                "turf_circumference": 1640,
                "dirt_circumference": 1487,
                "turf_straight": 266,
                "dirt_straight": 264,
            },
            "函館": {
                "turf_circumference": 1626,
                "dirt_circumference": 1475,
                "turf_straight": 262,
                "dirt_straight": 260,
            },
        }

        data = course_data.get(venue, {})
        conditions = []

        condition = self.get_course_conditions(venue)
        if condition:
            conditions.append(condition)

        return Course(
            venue_code=venue_code,
            venue_name=venue,
            turf_circumference=data.get("turf_circumference"),
            dirt_circumference=data.get("dirt_circumference"),
            turf_straight=data.get("turf_straight"),
            dirt_straight=data.get("dirt_straight"),
            conditions=conditions,
        )
