"""Netkeiba website scraper."""

import logging
import re
from datetime import date, datetime

from bs4 import BeautifulSoup, Tag

from backend.models.course import CourseCondition
from backend.models.horse import Horse, HorsePerformance
from backend.models.race import Race, RaceCard, RaceEntry, RaceResult, RaceSchedule
from backend.scrapers.base_scraper import BaseScraper
from backend.utils.date_utils import (
    get_current_week_dates,
    parse_japanese_date,
)

logger = logging.getLogger(__name__)


class NetkeibaScraper(BaseScraper):
    """Scraper for netkeiba website (https://race.netkeiba.com)."""

    BASE_URL = "https://race.netkeiba.com"
    DB_URL = "https://db.netkeiba.com"
    ENCODING = "euc-jp"

    VENUE_CODES = {
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

    def get_weekly_races(self) -> RaceSchedule:
        """Get this week's race schedule from netkeiba.

        Returns:
            RaceSchedule containing this week's races
        """
        week_start, week_end = get_current_week_dates()
        races: list[Race] = []

        try:
            soup = self.fetch_page(f"{self.BASE_URL}/top/")

            race_links = soup.find_all("a", href=re.compile(r"race_id=\d+"))
            seen_ids = set()

            for link in race_links:
                href = link.get("href", "")
                race_id = self._extract_race_id_from_url(href)

                if race_id and race_id not in seen_ids:
                    seen_ids.add(race_id)
                    race = self._parse_race_link(link, race_id)
                    if race:
                        races.append(race)

            race_tables = soup.find_all("table", class_=re.compile(r"race|Race"))
            for table in race_tables:
                races.extend(self._parse_race_table(table, week_start))

            race_list_divs = soup.find_all("div", class_=re.compile(r"RaceList|race_list"))
            for div in race_list_divs:
                races.extend(self._parse_race_list_div(div, week_start))

        except Exception as e:
            logger.error(f"Failed to fetch weekly races: {e}")

        unique_races = {r.race_id: r for r in races}

        return RaceSchedule(
            week_start=week_start,
            week_end=week_end,
            races=list(unique_races.values()),
            fetched_at=datetime.now(),
        )

    def get_race_card(self, race_id: str) -> RaceCard | None:
        """Get race card (shutuba-hyou) for a specific race.

        Args:
            race_id: Race ID

        Returns:
            RaceCard with entries or None
        """
        try:
            url = f"{self.BASE_URL}/race/shutuba.html"
            soup = self.fetch_page(url, params={"race_id": race_id})

            race = self._parse_race_info(soup, race_id)
            if not race:
                return None

            entries = self._parse_race_entries(soup)

            return RaceCard(race=race, entries=entries)

        except Exception as e:
            logger.error(f"Failed to fetch race card for {race_id}: {e}")
            return None

    def get_race_result(self, race_id: str) -> list[RaceResult] | None:
        """Get race results for a specific race.

        Args:
            race_id: Race ID

        Returns:
            List of RaceResult or None
        """
        try:
            url = f"{self.BASE_URL}/race/result.html"
            soup = self.fetch_page(url, params={"race_id": race_id})

            return self._parse_race_results(soup)

        except Exception as e:
            logger.error(f"Failed to fetch race result for {race_id}: {e}")
            return None

    def get_horse_info(self, horse_id: str) -> Horse | None:
        """Get detailed horse information.

        Args:
            horse_id: Horse ID

        Returns:
            Horse information or None
        """
        try:
            url = f"{self.DB_URL}/horse/{horse_id}"
            soup = self.fetch_page(url)

            return self._parse_horse_page(soup, horse_id)

        except Exception as e:
            logger.error(f"Failed to fetch horse info for {horse_id}: {e}")
            return None

    def get_horse_performances(
        self, horse_id: str, limit: int = 10
    ) -> list[HorsePerformance]:
        """Get horse's past race performances.

        Args:
            horse_id: Horse ID
            limit: Maximum number of performances to return

        Returns:
            List of past performances
        """
        try:
            url = f"{self.DB_URL}/horse/{horse_id}"
            soup = self.fetch_page(url)

            return self._parse_horse_performances(soup, limit)

        except Exception as e:
            logger.error(f"Failed to fetch horse performances for {horse_id}: {e}")
            return []

    def get_track_condition(self, venue_code: str) -> CourseCondition | None:
        """Get current track condition for a venue.

        Args:
            venue_code: Venue code (01-10)

        Returns:
            CourseCondition or None
        """
        try:
            soup = self.fetch_page(f"{self.BASE_URL}/top/")

            venue_name = self.VENUE_CODES.get(venue_code, "")
            condition_section = soup.find(
                string=re.compile(rf"{venue_name}.*馬場|馬場.*{venue_name}")
            )

            if condition_section:
                return self._parse_track_condition(condition_section, venue_name)

            return None

        except Exception as e:
            logger.error(f"Failed to fetch track condition for {venue_code}: {e}")
            return None

    def _extract_race_id_from_url(self, url: str) -> str | None:
        """Extract race ID from URL."""
        match = re.search(r"race_id=(\d+)", url)
        if match:
            return match.group(1)

        match = re.search(r"/race/(\d+)", url)
        if match:
            return match.group(1)

        return None

    def _parse_race_link(self, link: Tag, race_id: str) -> Race | None:
        """Parse race from link element."""
        try:
            text = self.clean_text(link.get_text())

            race_number = self._extract_race_number(race_id)
            race_date = self._extract_date_from_race_id(race_id)
            venue_code = self._extract_venue_code_from_race_id(race_id)
            venue = self.VENUE_CODES.get(venue_code, "不明")

            race_name = self._extract_race_name(text) or f"{race_number}R"
            distance, track_type = self._extract_distance_info(text)

            return Race(
                race_id=race_id,
                race_name=race_name,
                race_number=race_number,
                race_date=race_date or date.today(),
                venue=venue,
                venue_code=venue_code,
                distance=distance or 0,
                track_type=track_type or "不明",
            )

        except Exception as e:
            logger.warning(f"Failed to parse race link: {e}")
            return None

    def _parse_race_table(self, table: Tag, default_date: date) -> list[Race]:
        """Parse race table for race information."""
        races = []

        rows = table.find_all("tr")
        for row in rows:
            link = row.find("a", href=re.compile(r"race_id="))
            if link:
                href = link.get("href", "")
                race_id = self._extract_race_id_from_url(href)
                if race_id:
                    race = self._parse_race_link(link, race_id)
                    if race:
                        races.append(race)

        return races

    def _parse_race_list_div(self, div: Tag, default_date: date) -> list[Race]:
        """Parse race list div for race information."""
        races = []

        links = div.find_all("a", href=re.compile(r"race_id=|/race/\d+"))
        for link in links:
            href = link.get("href", "")
            race_id = self._extract_race_id_from_url(href)
            if race_id:
                race = self._parse_race_link(link, race_id)
                if race:
                    races.append(race)

        return races

    def _parse_race_info(self, soup: BeautifulSoup, race_id: str) -> Race | None:
        """Parse race information from race page."""
        try:
            race_name_elem = soup.find(class_=re.compile(r"RaceName|race_name"))
            race_name = (
                self.clean_text(race_name_elem.get_text())
                if race_name_elem
                else f"Race {race_id}"
            )

            race_data_elem = soup.find(class_=re.compile(r"RaceData|race_data"))
            race_data_text = (
                self.clean_text(race_data_elem.get_text()) if race_data_elem else ""
            )

            race_number = self._extract_race_number(race_id)
            race_date = self._extract_date_from_race_id(race_id) or date.today()
            venue_code = self._extract_venue_code_from_race_id(race_id)
            venue = self.VENUE_CODES.get(venue_code, "不明")
            distance, track_type = self._extract_distance_info(race_data_text)

            track_condition = None
            for cond in ["良", "稍重", "重", "不良"]:
                if cond in race_data_text:
                    track_condition = cond
                    break

            weather = None
            for w in ["晴", "曇", "雨", "小雨", "雪"]:
                if w in race_data_text:
                    weather = w
                    break

            post_time = None
            time_match = re.search(r"(\d{1,2}:\d{2})", race_data_text)
            if time_match:
                post_time = time_match.group(1)

            return Race(
                race_id=race_id,
                race_name=race_name,
                race_number=race_number,
                race_date=race_date,
                venue=venue,
                venue_code=venue_code,
                distance=distance or 0,
                track_type=track_type or "不明",
                track_condition=track_condition,
                weather=weather,
                post_time=post_time,
            )

        except Exception as e:
            logger.warning(f"Failed to parse race info: {e}")
            return None

    def _parse_race_entries(self, soup: BeautifulSoup) -> list[RaceEntry]:
        """Parse race entries from shutuba page."""
        entries = []

        table = soup.find("table", class_=re.compile(r"Shutuba|shutuba|race_table"))
        if not table:
            table = soup.find("table", id=re.compile(r"shutuba"))
        if not table:
            return entries

        rows = table.find_all("tr")
        for row in rows:
            cols = row.find_all(["td", "th"])
            if len(cols) < 4:
                continue

            try:
                entry = self._parse_entry_row(cols)
                if entry:
                    entries.append(entry)
            except Exception as e:
                logger.warning(f"Failed to parse entry row: {e}")

        return entries

    def _parse_entry_row(self, cols: list[Tag]) -> RaceEntry | None:
        """Parse a single entry row."""
        try:
            bracket_text = self.clean_text(cols[0].get_text())
            bracket_number = self.extract_number(bracket_text)
            if not bracket_number:
                return None

            horse_number_text = self.clean_text(cols[1].get_text())
            horse_number = self.extract_number(horse_number_text)
            if not horse_number:
                return None

            horse_cell = cols[3] if len(cols) > 3 else cols[2]
            horse_link = horse_cell.find("a")
            horse_name = self.clean_text(horse_cell.get_text())

            horse_id = None
            if horse_link:
                href = horse_link.get("href", "")
                match = re.search(r"horse/(\d+)", href)
                if match:
                    horse_id = match.group(1)

            sex = None
            age = None
            if len(cols) > 4:
                sex_age_text = self.clean_text(cols[4].get_text())
                sex_match = re.match(r"([牡牝セ騸])", sex_age_text)
                if sex_match:
                    sex = sex_match.group(1)
                age_match = re.search(r"(\d+)", sex_age_text)
                if age_match:
                    age = int(age_match.group(1))

            weight = None
            if len(cols) > 5:
                weight = self.extract_float(cols[5].get_text())

            jockey_name = None
            jockey_id = None
            if len(cols) > 6:
                jockey_cell = cols[6]
                jockey_link = jockey_cell.find("a")
                jockey_name = self.clean_text(jockey_cell.get_text())
                if jockey_link:
                    href = jockey_link.get("href", "")
                    match = re.search(r"jockey/(\d+)", href)
                    if match:
                        jockey_id = match.group(1)

            trainer_name = None
            trainer_id = None
            if len(cols) > 7:
                trainer_cell = cols[7]
                trainer_link = trainer_cell.find("a")
                trainer_name = self.clean_text(trainer_cell.get_text())
                if trainer_link:
                    href = trainer_link.get("href", "")
                    match = re.search(r"trainer/(\d+)", href)
                    if match:
                        trainer_id = match.group(1)

            return RaceEntry(
                bracket_number=bracket_number,
                horse_number=horse_number,
                horse_name=horse_name,
                horse_id=horse_id,
                sex=sex,
                age=age,
                weight=weight,
                jockey_name=jockey_name,
                jockey_id=jockey_id,
                trainer_name=trainer_name,
                trainer_id=trainer_id,
            )

        except Exception as e:
            logger.warning(f"Failed to parse entry: {e}")
            return None

    def _parse_race_results(self, soup: BeautifulSoup) -> list[RaceResult]:
        """Parse race results from result page."""
        results = []

        table = soup.find("table", class_=re.compile(r"Result|result|race_table"))
        if not table:
            return results

        rows = table.find_all("tr")
        for row in rows:
            cols = row.find_all(["td"])
            if len(cols) < 4:
                continue

            try:
                result = self._parse_result_row(cols)
                if result:
                    results.append(result)
            except Exception as e:
                logger.warning(f"Failed to parse result row: {e}")

        return results

    def _parse_result_row(self, cols: list[Tag]) -> RaceResult | None:
        """Parse a single result row."""
        try:
            rank_text = self.clean_text(cols[0].get_text())
            rank = self.extract_number(rank_text)

            bracket_text = self.clean_text(cols[1].get_text())
            bracket_number = self.extract_number(bracket_text)
            if not bracket_number:
                return None

            horse_number_text = self.clean_text(cols[2].get_text())
            horse_number = self.extract_number(horse_number_text)
            if not horse_number:
                return None

            horse_cell = cols[3]
            horse_link = horse_cell.find("a")
            horse_name = self.clean_text(horse_cell.get_text())

            horse_id = None
            if horse_link:
                href = horse_link.get("href", "")
                match = re.search(r"horse/(\d+)", href)
                if match:
                    horse_id = match.group(1)

            finish_time = None
            if len(cols) > 7:
                finish_time = self.clean_text(cols[7].get_text())

            margin = None
            if len(cols) > 8:
                margin = self.clean_text(cols[8].get_text())

            passing_order = None
            if len(cols) > 10:
                passing_order = self.clean_text(cols[10].get_text())

            last_3f = None
            if len(cols) > 11:
                last_3f = self.extract_float(cols[11].get_text())

            jockey_name = None
            if len(cols) > 6:
                jockey_name = self.clean_text(cols[6].get_text())

            odds = None
            popularity = None
            if len(cols) > 12:
                odds = self.extract_float(cols[12].get_text())
            if len(cols) > 13:
                popularity = self.extract_number(cols[13].get_text())

            return RaceResult(
                rank=rank,
                bracket_number=bracket_number,
                horse_number=horse_number,
                horse_name=horse_name,
                horse_id=horse_id,
                finish_time=finish_time,
                margin=margin,
                passing_order=passing_order,
                last_3f=last_3f,
                jockey_name=jockey_name,
                odds=odds,
                popularity=popularity,
            )

        except Exception as e:
            logger.warning(f"Failed to parse result: {e}")
            return None

    def _parse_horse_page(self, soup: BeautifulSoup, horse_id: str) -> Horse | None:
        """Parse horse information from horse page."""
        try:
            name_elem = soup.find(class_=re.compile(r"horse_name|HorseName"))
            horse_name = self.clean_text(name_elem.get_text()) if name_elem else "不明"

            profile_table = soup.find("table", class_=re.compile(r"profile|db_prof"))
            profile_data = {}
            if profile_table:
                rows = profile_table.find_all("tr")
                for row in rows:
                    th = row.find("th")
                    td = row.find("td")
                    if th and td:
                        key = self.clean_text(th.get_text())
                        value = self.clean_text(td.get_text())
                        profile_data[key] = value

            sex = profile_data.get("性別", "不明")
            if sex.startswith("牡"):
                sex = "牡"
            elif sex.startswith("牝"):
                sex = "牝"
            elif sex.startswith("セ") or sex.startswith("騸"):
                sex = "セ"

            birth_year = None
            birth_str = profile_data.get("生年月日", "")
            year_match = re.search(r"(\d{4})", birth_str)
            if year_match:
                birth_year = int(year_match.group(1))

            sire_name = profile_data.get("父", None)
            dam_name = profile_data.get("母", None)
            damsire_name = profile_data.get("母父", None)

            blood_table = soup.find("table", class_=re.compile(r"blood"))
            if blood_table and not sire_name:
                links = blood_table.find_all("a")
                if len(links) >= 1:
                    sire_name = self.clean_text(links[0].get_text())
                if len(links) >= 2:
                    dam_name = self.clean_text(links[1].get_text())

            trainer_name = profile_data.get("調教師", None)
            owner_name = profile_data.get("馬主", None)
            breeder_name = profile_data.get("生産者", None)

            training_center = None
            if trainer_name:
                if "美浦" in trainer_name or "(美)" in trainer_name:
                    training_center = "美浦"
                elif "栗東" in trainer_name or "(栗)" in trainer_name:
                    training_center = "栗東"

            performances = self._parse_horse_performances(soup, limit=20)

            total_races = len(performances)
            total_wins = sum(1 for p in performances if p.rank == 1)

            return Horse(
                horse_id=horse_id,
                horse_name=horse_name,
                sex=sex,
                birth_year=birth_year,
                sire_name=sire_name,
                dam_name=dam_name,
                damsire_name=damsire_name,
                trainer_name=trainer_name,
                owner_name=owner_name,
                breeder_name=breeder_name,
                training_center=training_center,
                total_races=total_races,
                total_wins=total_wins,
                performances=performances,
            )

        except Exception as e:
            logger.error(f"Failed to parse horse page: {e}")
            return None

    def _parse_horse_performances(
        self, soup: BeautifulSoup, limit: int = 10
    ) -> list[HorsePerformance]:
        """Parse horse performance history."""
        performances = []

        result_table = soup.find(
            "table", class_=re.compile(r"result|nk_tb_common|db_h_race")
        )
        if not result_table:
            return performances

        rows = result_table.find_all("tr")
        for row in rows[:limit + 1]:
            cols = row.find_all("td")
            if len(cols) < 10:
                continue

            try:
                perf = self._parse_performance_row(cols)
                if perf:
                    performances.append(perf)
            except Exception as e:
                logger.warning(f"Failed to parse performance row: {e}")

        return performances[:limit]

    def _parse_performance_row(self, cols: list[Tag]) -> HorsePerformance | None:
        """Parse a single performance row."""
        try:
            date_text = self.clean_text(cols[0].get_text())
            race_date = parse_japanese_date(date_text)
            if not race_date:
                return None

            venue = self.clean_text(cols[1].get_text())

            race_name = self.clean_text(cols[4].get_text()) if len(cols) > 4 else ""

            distance = 0
            track_type = "不明"
            if len(cols) > 14:
                dist_text = self.clean_text(cols[14].get_text())
                distance = self.extract_number(dist_text) or 0
                if "芝" in dist_text:
                    track_type = "芝"
                elif "ダ" in dist_text:
                    track_type = "ダート"

            rank = self.extract_number(cols[11].get_text()) if len(cols) > 11 else None

            jockey_name = self.clean_text(cols[12].get_text()) if len(cols) > 12 else None

            weight = self.extract_float(cols[13].get_text()) if len(cols) > 13 else None

            entry_count = self.extract_number(cols[6].get_text()) if len(cols) > 6 else None
            horse_number = self.extract_number(cols[8].get_text()) if len(cols) > 8 else None
            odds = self.extract_float(cols[9].get_text()) if len(cols) > 9 else None
            popularity = self.extract_number(cols[10].get_text()) if len(cols) > 10 else None

            finish_time = self.clean_text(cols[17].get_text()) if len(cols) > 17 else None
            margin = self.clean_text(cols[18].get_text()) if len(cols) > 18 else None
            last_3f = self.extract_float(cols[22].get_text()) if len(cols) > 22 else None

            track_condition = None
            weather = None
            if len(cols) > 15:
                cond_text = self.clean_text(cols[15].get_text())
                for c in ["良", "稍", "重", "不"]:
                    if c in cond_text:
                        track_condition = c
                        if c == "稍":
                            track_condition = "稍重"
                        elif c == "不":
                            track_condition = "不良"
                        break

            return HorsePerformance(
                race_date=race_date,
                race_name=race_name,
                venue=venue,
                distance=distance,
                track_type=track_type,
                track_condition=track_condition,
                weather=weather,
                entry_count=entry_count,
                horse_number=horse_number,
                rank=rank,
                odds=odds,
                popularity=popularity,
                jockey_name=jockey_name,
                weight=weight,
                finish_time=finish_time,
                margin=margin,
                last_3f=last_3f,
            )

        except Exception as e:
            logger.warning(f"Failed to parse performance: {e}")
            return None

    def _parse_track_condition(
        self, element: Tag, venue: str
    ) -> CourseCondition | None:
        """Parse track condition from element."""
        text = self.clean_text(str(element))

        turf_condition = None
        dirt_condition = None

        for condition in ["良", "稍重", "重", "不良"]:
            if f"芝{condition}" in text:
                turf_condition = condition
            if f"ダート{condition}" in text or f"ダ{condition}" in text:
                dirt_condition = condition

        return CourseCondition(
            venue=venue,
            condition_date=date.today(),
            turf_condition=turf_condition,
            dirt_condition=dirt_condition,
        )

    def _extract_race_number(self, race_id: str) -> int:
        """Extract race number from race ID."""
        if len(race_id) >= 12:
            try:
                return int(race_id[10:12])
            except ValueError:
                pass
        return 1

    def _extract_date_from_race_id(self, race_id: str) -> date | None:
        """Extract date from race ID."""
        if len(race_id) >= 8:
            try:
                year = int(race_id[:4])
                month = int(race_id[4:6])
                day = int(race_id[6:8])
                return date(year, month, day)
            except ValueError:
                pass
        return None

    def _extract_venue_code_from_race_id(self, race_id: str) -> str:
        """Extract venue code from race ID."""
        if len(race_id) >= 10:
            return race_id[8:10]
        return "00"

    def _extract_race_name(self, text: str) -> str | None:
        """Extract race name from text."""
        text = re.sub(r"^\d+R\s*", "", text)
        text = re.sub(r"\s*(芝|ダート?)\s*\d+m.*$", "", text)
        return text.strip() if text.strip() else None

    def _extract_distance_info(self, text: str) -> tuple[int | None, str | None]:
        """Extract distance and track type from text."""
        distance = None
        track_type = None

        distance_match = re.search(r"(\d{3,4})\s*m", text)
        if distance_match:
            distance = int(distance_match.group(1))

        if "芝" in text:
            track_type = "芝"
        elif "ダート" in text or re.search(r"ダ\d", text):
            track_type = "ダート"
        elif "障" in text:
            track_type = "障害"

        return distance, track_type
