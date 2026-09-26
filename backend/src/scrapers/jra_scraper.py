"""JRA official website scraper."""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import unquote

from bs4 import BeautifulSoup, Tag

from backend.models.course import Course, CourseCondition, WeeklyVenueSchedule
from backend.models.race import Race, RaceCard, RaceEntry, RaceSchedule
from backend.scrapers.base_scraper import BaseScraper
from backend.utils.date_utils import get_current_week_dates, parse_japanese_date
from backend.utils.http_client import HTTPClient

logger = logging.getLogger(__name__)

_ACCESS_D_ACTION = re.compile(
    r"""doAction\(\s*['"]([^'"]*accessD\.html)['"]\s*,\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
_CNAME_PARAM = re.compile(r"(?:CNAME|cname)=([A-Za-z0-9/%]+)")
_RACE_CNAME = re.compile(
    r"^pw01dde01(?P<venue>\d{2})(?P<year>\d{4})(?P<kai>\d{2})"
    r"(?P<day>\d{2})(?P<race>\d{2})(?P<ymd>\d{8})/[0-9A-Fa-f]{2}$"
)
_MEETING_CNAME = re.compile(
    r"^pw01drl00(?P<venue>\d{2})(?P<year>\d{4})(?P<kai>\d{2})"
    r"(?P<day>\d{2})(?P<ymd>\d{8})/[0-9A-Fa-f]{2}$"
)


@dataclass(frozen=True)
class _ParsedRaceCname:
    """Race card token from accessD."""

    cname: str
    venue_code: str
    year: int
    kai: int
    day_number: int
    race_number: int
    race_date: date


@dataclass(frozen=True)
class _ParsedMeetingCname:
    """Meeting list token from accessD."""

    cname: str
    venue_code: str
    year: int
    kai: int
    day_number: int
    race_date: date


@dataclass(frozen=True)
class _RaceLookup:
    """Fields used to match a race_id against accessD tokens."""

    race_id: str
    venue_code: str
    race_number: int
    year: int | None = None
    kai: int | None = None
    day_number: int | None = None
    race_date: date | None = None
    netkeiba_format: bool = False

    def matches_race(self, race: _ParsedRaceCname) -> bool:
        """Return whether this lookup selects the race token."""
        if race.venue_code != self.venue_code or race.race_number != self.race_number:
            return False
        if self.netkeiba_format:
            return (
                race.year == self.year
                and race.kai == self.kai
                and race.day_number == self.day_number
            )
        return race.race_date == self.race_date

    def matches_meeting(self, meeting: _ParsedMeetingCname) -> bool:
        """Return whether the meeting page can contain this race."""
        if meeting.venue_code != self.venue_code:
            return False
        if self.netkeiba_format:
            return (
                meeting.year == self.year
                and meeting.kai == self.kai
                and meeting.day_number == self.day_number
            )
        return meeting.race_date == self.race_date


class JRAScraper(BaseScraper):
    """Scraper for JRA official website (https://jra.jp)."""

    BASE_URL = "https://jra.jp"
    ENCODING = "utf-8"
    ACCESS_D_URL = "https://www.jra.go.jp/JRADB/accessD.html"
    ACCESS_D_ENCODING = "cp932"
    SHUTUBA_INDEX_CNAME = "pw01dli00/F3"

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

    def __init__(self, http_client: HTTPClient | None = None):
        """Initialize scraper and the accessD page cache."""
        super().__init__(http_client)
        self._access_d_html: dict[str, str] = {}

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

    def get_race_card(self, race_id: str) -> RaceCard | None:
        """Get a race card from JRA accessD.

        Walks the shutuba menu (``pw01dli00/F3``) and follows only the meeting
        whose date or kai/day matches ``race_id``. The checksum suffix is taken
        from the page instead of being computed.

        Args:
            race_id: Project ID (YYYYMMDD + venue + race) or netkeiba ID
                (year + venue + kai + day + race)

        Returns:
            RaceCard or None when the race is not on the current menu
        """
        lookup = self._lookup_from_race_id(race_id)
        if lookup is None:
            logger.info("Race id %s cannot be mapped to an accessD card", race_id)
            return None

        try:
            cname = self._find_race_cname(lookup)
            if cname is None:
                logger.info("No accessD card found for %s", race_id)
                return None
            html = self._fetch_access_d(cname.cname)
            return self._parse_race_card(html, race_id, cname)
        except Exception as e:
            logger.error(f"Failed to fetch JRA race card for {race_id}: {e}")
            return None

    def _fetch_access_d(self, cname: str) -> str:
        """POST one accessD command and cache the HTML."""
        cached = self._access_d_html.get(cname)
        if cached is not None:
            return cached
        html = self.http_client.post(
            self.ACCESS_D_URL,
            data={"cname": cname},
            encoding=self.ACCESS_D_ENCODING,
        )
        self._access_d_html[cname] = html
        return html

    def _find_race_cname(self, lookup: _RaceLookup) -> _ParsedRaceCname | None:
        """Find the race token within two hops of the shutuba index."""
        index_html = self._fetch_access_d(self.SHUTUBA_INDEX_CNAME)
        races, meetings = self._scan_access_d(index_html)
        matched = self._first_matching_race(races, lookup)
        if matched is not None:
            return matched

        for meeting in meetings:
            if not lookup.matches_meeting(meeting):
                continue
            meeting_html = self._fetch_access_d(meeting.cname)
            meeting_races, _nested = self._scan_access_d(meeting_html)
            matched = self._first_matching_race(meeting_races, lookup)
            if matched is not None:
                return matched
        return None

    @staticmethod
    def _first_matching_race(
        races: list[_ParsedRaceCname], lookup: _RaceLookup
    ) -> _ParsedRaceCname | None:
        """Return the first race token selected by the lookup."""
        for race in races:
            if lookup.matches_race(race):
                return race
        return None

    def _lookup_from_race_id(self, race_id: str) -> _RaceLookup | None:
        """Interpret a race id as netkeiba or project format.

        Netkeiba IDs are preferred when the venue, kai (1-6) and meeting day
        (1-12) fields are all plausible. That matches NetkeibaScraper.
        """
        if len(race_id) < 12 or not race_id[:12].isdigit():
            return None

        body = race_id[:12]
        race_number = int(body[10:12])
        if not 1 <= race_number <= 12:
            return None

        venue_code = body[4:6]
        kai = int(body[6:8])
        day_number = int(body[8:10])
        if venue_code in self.VENUE_CODES.values() and 1 <= kai <= 6 and 1 <= day_number <= 12:
            return _RaceLookup(
                race_id=race_id,
                venue_code=venue_code,
                race_number=race_number,
                year=int(body[:4]),
                kai=kai,
                day_number=day_number,
                netkeiba_format=True,
            )

        try:
            race_date = date(int(body[:4]), int(body[4:6]), int(body[6:8]))
        except ValueError:
            return None

        project_venue = body[8:10]
        if project_venue not in self.VENUE_CODES.values():
            return None
        return _RaceLookup(
            race_id=race_id,
            venue_code=project_venue,
            race_number=race_number,
            race_date=race_date,
        )

    def _scan_access_d(
        self, html: str
    ) -> tuple[list[_ParsedRaceCname], list[_ParsedMeetingCname]]:
        """Collect race and meeting tokens from an accessD page."""
        races: list[_ParsedRaceCname] = []
        meetings: list[_ParsedMeetingCname] = []
        seen: set[str] = set()
        for token in self._extract_access_d_cnames(html):
            if token in seen or token == self.SHUTUBA_INDEX_CNAME:
                continue
            seen.add(token)
            race = self._parse_race_cname(token)
            if race is not None:
                races.append(race)
                continue
            meeting = self._parse_meeting_cname(token)
            if meeting is not None:
                meetings.append(meeting)
        return races, meetings

    @staticmethod
    def _extract_access_d_cnames(html: str) -> list[str]:
        """Extract accessD cname values from doAction calls and query strings."""
        tokens: list[str] = []
        for match in _ACCESS_D_ACTION.finditer(html):
            tokens.append(unquote(match.group(2)).strip())
        for match in _CNAME_PARAM.finditer(html):
            tokens.append(unquote(match.group(1)).strip())
        return tokens

    def _parse_race_cname(self, token: str) -> _ParsedRaceCname | None:
        """Parse a race-card cname, ignoring the checksum."""
        match = _RACE_CNAME.match(token)
        if not match:
            return None
        venue_code = match.group("venue")
        race_number = int(match.group("race"))
        if venue_code not in self.VENUE_CODES.values() or not 1 <= race_number <= 12:
            return None
        ymd = match.group("ymd")
        try:
            race_date = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
        except ValueError:
            return None
        return _ParsedRaceCname(
            cname=token,
            venue_code=venue_code,
            year=int(match.group("year")),
            kai=int(match.group("kai")),
            day_number=int(match.group("day")),
            race_number=race_number,
            race_date=race_date,
        )

    def _parse_meeting_cname(self, token: str) -> _ParsedMeetingCname | None:
        """Parse a meeting-list cname, ignoring the checksum."""
        match = _MEETING_CNAME.match(token)
        if not match:
            return None
        venue_code = match.group("venue")
        if venue_code not in self.VENUE_CODES.values():
            return None
        ymd = match.group("ymd")
        try:
            race_date = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
        except ValueError:
            return None
        return _ParsedMeetingCname(
            cname=token,
            venue_code=venue_code,
            year=int(match.group("year")),
            kai=int(match.group("kai")),
            day_number=int(match.group("day")),
            race_date=race_date,
        )

    def _parse_race_card(
        self, html: str, race_id: str, cname: _ParsedRaceCname
    ) -> RaceCard | None:
        """Parse a shutuba page into a race card."""
        soup = BeautifulSoup(html, "lxml")
        entries = self._parse_shutuba_entries(soup)
        if not entries:
            return None

        race = self._parse_shutuba_race(soup, race_id, cname, len(entries))
        return RaceCard(race=race, entries=entries)

    def _parse_shutuba_race(
        self,
        soup: BeautifulSoup,
        race_id: str,
        cname: _ParsedRaceCname,
        entry_count: int,
    ) -> Race:
        """Parse race conditions from the shutuba header."""
        header = soup.select_one("div.race_header") or soup
        header_text = self.clean_text(header.get_text(" ", strip=True))

        race_name = self._header_text(header, ".race_name") or f"{cname.race_number}R"
        race_number = self._header_race_number(header) or cname.race_number
        race_date = parse_japanese_date(header_text) or cname.race_date
        venue = self._venue_name(cname.venue_code)
        round_number, day_number = self._header_meeting(header_text)
        post_time = self._header_post_time(header)
        distance, track_type, direction = self._header_course(header)
        prize_money = self._header_prize(header)

        return Race(
            race_id=race_id,
            race_name=race_name,
            race_number=race_number,
            race_date=race_date,
            post_time=post_time,
            venue=venue,
            venue_code=cname.venue_code,
            round_number=round_number or cname.kai,
            day_number=day_number or cname.day_number,
            distance=distance or 0,
            track_type=track_type or "不明",
            direction=direction,
            race_class=self._header_text(header, ".type .cell.class"),
            race_grade=self._header_grade(header),
            age_condition=self._header_text(header, ".type .cell.category"),
            sex_condition=self._header_sex_condition(header),
            weight_condition=self._header_text(header, ".type .cell.weight"),
            prize_money=prize_money,
            entry_count=entry_count,
        )

    def _parse_shutuba_entries(self, soup: BeautifulSoup) -> list[RaceEntry]:
        """Parse starter rows from the shutuba table."""
        entries: list[RaceEntry] = []
        for row in soup.select("table tr"):
            if row.select_one("td.waku") is None or row.select_one("td.horse") is None:
                continue
            entry = self._parse_shutuba_row(row)
            if entry is not None:
                entries.append(entry)
        return entries

    def _parse_shutuba_row(self, row: Tag) -> RaceEntry | None:
        """Parse one starter row."""
        bracket_number = self._bracket_number(row.select_one("td.waku"))
        horse_number = self.extract_number(self._cell_text(row, "td.num"))
        horse_cell = row.select_one("td.horse")
        jockey_cell = row.select_one("td.jockey")
        if (
            bracket_number is None
            or horse_number is None
            or horse_cell is None
            or jockey_cell is None
        ):
            return None

        name_node = horse_cell.select_one(".name")
        horse_name = self.clean_text(name_node.get_text(" ", strip=True) if name_node else "")
        if not horse_name:
            return None

        sex, age = self._parse_sex_age(self._node_text(jockey_cell.select_one("p.age")))
        weight_diff = self._horse_weight_diff(horse_cell)
        return RaceEntry(
            bracket_number=bracket_number,
            horse_number=horse_number,
            horse_name=horse_name,
            sex=sex,
            age=age,
            weight=self.extract_float(self._node_text(jockey_cell.select_one("p.weight"))),
            jockey_name=self._person_name(jockey_cell.select_one("p.jockey")),
            trainer_name=self._person_name(horse_cell.select_one("p.trainer")),
            owner_name=self._node_text(horse_cell.select_one("p.owner")) or None,
            horse_weight=self._horse_weight(horse_cell),
            horse_weight_diff=weight_diff,
            odds=self._odds(horse_cell),
            popularity=self._popularity(horse_cell),
        )

    def _venue_name(self, venue_code: str) -> str:
        """Return the Japanese venue name for a code."""
        for name, code in self.VENUE_CODES.items():
            if code == venue_code:
                return name
        return "不明"

    def _header_text(self, header: Tag, selector: str) -> str | None:
        """Return cleaned text for a header selector."""
        node = header.select_one(selector)
        text = self._node_text(node)
        return text or None

    def _header_race_number(self, header: Tag) -> int | None:
        """Read the race number from the header image alt text."""
        image = header.select_one(".race_number img")
        if image is None:
            return None
        match = re.search(r"(\d+)\s*レース", str(image.get("alt") or ""))
        if match:
            number = int(match.group(1))
            if 1 <= number <= 12:
                return number
        return None

    def _header_meeting(self, text: str) -> tuple[int | None, int | None]:
        """Extract kai and day from text such as ``4回中山8日``."""
        venues = "|".join(self.VENUE_CODES.keys())
        match = re.search(rf"(\d+)\s*回\s*(?:{venues})\s*(\d+)\s*日", text)
        if not match:
            return None, None
        return int(match.group(1)), int(match.group(2))

    def _header_post_time(self, header: Tag) -> str | None:
        """Convert ``15時30分`` to ``15:30``."""
        node = header.select_one(".cell.time")
        text = self._node_text(node)
        match = re.search(r"(\d{1,2})\s*時\s*(\d{1,2})\s*分", text)
        if not match:
            return None
        return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"

    def _header_course(self, header: Tag) -> tuple[int | None, str | None, str | None]:
        """Extract distance, surface and direction from the course cell."""
        text = self._node_text(header.select_one(".type .cell.course"))
        distance = None
        distance_match = re.search(r"([\d,]+)\s*メートル", text)
        if distance_match:
            distance = int(distance_match.group(1).replace(",", ""))

        if "障" in text:
            track_type = "障害"
        elif "ダート" in text or "ダ" in text:
            track_type = "ダート"
        elif "芝" in text:
            track_type = "芝"
        else:
            track_type = None

        direction = None
        if "直線" in text:
            direction = "直線"
        elif "右" in text:
            direction = "右"
        elif "左" in text:
            direction = "左"
        return distance, track_type, direction

    def _header_prize(self, header: Tag) -> int | None:
        """Return the winner's purse in units of 10,000 yen."""
        for unit in header.select("ul.prize .prize_unit"):
            caption = self._node_text(unit.select_one(".cell.cap"))
            if "本賞金" not in caption:
                continue
            for item in unit.select("ol li"):
                label = self._node_text(item.select_one(".cap"))
                if "1着" not in label:
                    continue
                amount = self._node_text(item.select_one(".num")).replace(",", "")
                if not amount:
                    return None
                try:
                    return int(float(amount))
                except ValueError:
                    return None
        return None

    def _header_grade(self, header: Tag) -> str | None:
        """Return a grade label when the header marks one."""
        for node in header.select("[class*='grade']"):
            classes = " ".join(node.get("class") or [])
            if "no_grade" in classes:
                continue
            text = self.clean_text(node.get_text(" ", strip=True))
            if text:
                return text
            alt = node.get("alt")
            if isinstance(alt, str) and alt.strip():
                return alt.strip()
        return None

    def _header_sex_condition(self, header: Tag) -> str | None:
        """Return 混合 / 牝 / 牡 / セ when the rule cell states it."""
        rule = self._node_text(header.select_one(".type .cell.rule"))
        for token in ("混合", "牝", "牡", "セ"):
            if token in rule:
                return token
        return None

    def _bracket_number(self, cell: Tag | None) -> int | None:
        """Read a bracket number from alt text or cell text."""
        if cell is None:
            return None
        image = cell.find("img")
        alt = str(image.get("alt") or "") if image else ""
        match = re.search(r"枠\s*(\d)", alt) or re.search(r"(\d)", alt)
        if match:
            number = int(match.group(1))
            if 1 <= number <= 8:
                return number
        number = self.extract_number(self.clean_text(cell.get_text(" ", strip=True)))
        if number is not None and 1 <= number <= 8:
            return number
        return None

    def _horse_weight(self, horse_cell: Tag) -> int | None:
        """Read the body weight in kilograms."""
        text = self._node_text(horse_cell.select_one(".result_line .weight, .cell.weight"))
        match = re.search(r"(\d+)\s*kg", text, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None

    def _horse_weight_diff(self, horse_cell: Tag) -> int | None:
        """Read the body-weight change, such as ``(-4)``."""
        text = self._node_text(horse_cell.select_one(".transition"))
        match = re.search(r"\(([+-]?\d+)\)", text)
        if match:
            return int(match.group(1))
        return None

    def _odds(self, horse_cell: Tag) -> float | None:
        """Read win odds."""
        text = self._node_text(horse_cell.select_one(".odds .num"))
        if not text:
            return None
        try:
            return float(text.replace(",", ""))
        except ValueError:
            return self.extract_float(text)

    def _popularity(self, horse_cell: Tag) -> int | None:
        """Read popularity rank from ``(6番人気)``."""
        text = self._node_text(horse_cell.select_one(".pop_rank"))
        match = re.search(r"(\d+)\s*番人気", text)
        if match:
            return int(match.group(1))
        return None

    @staticmethod
    def _parse_sex_age(text: str) -> tuple[str | None, int | None]:
        """Split ``牡5/栗`` into sex and age."""
        match = re.search(r"([牡牝セ騸])\s*(\d+)", text)
        if not match:
            return None, None
        return match.group(1), int(match.group(2))

    def _person_name(self, node: Tag | None) -> str | None:
        """Return a jockey or trainer name without the stable division."""
        if node is None:
            return None
        link = node.find("a")
        source = link if link is not None else node
        text = self.clean_text(source.get_text(" ", strip=True))
        text = re.sub(r"\([^)]*\)", "", text).strip()
        return text or None

    def _cell_text(self, row: Tag, selector: str) -> str:
        """Return cleaned text for a cell in a row."""
        return self._node_text(row.select_one(selector))

    def _node_text(self, node: Tag | None) -> str:
        """Return cleaned text for a node."""
        if node is None:
            return ""
        return self.clean_text(node.get_text(" ", strip=True))
