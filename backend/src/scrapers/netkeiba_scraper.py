"""Netkeiba website scraper."""

import logging
import re
from dataclasses import dataclass, fields
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

HORSE_RESULT_FETCH_FAILED = "horse result fetch failed"
HORSE_RESULT_TABLE_NOT_FOUND = "horse result table not found"
HORSE_RESULT_TABLE_MALFORMED = "horse result table malformed"
HORSE_RESULT_EMPTY = "no performances on horse result page"

_PLACEHOLDERS = frozenset({"", "-", "--", "---", "**", "*", "----", "---.-"})
_RESULT_HEADERS = {
    "日付": "date",
    "開催": "venue",
    "天気": "weather",
    "レース名": "race_name",
    "頭数": "entry_count",
    "馬番": "horse_number",
    "オッズ": "odds",
    "人気": "popularity",
    "着順": "rank",
    "騎手": "jockey",
    "斤量": "weight",
    "距離": "distance",
    "馬場": "track_condition",
    "タイム": "finish_time",
    "着差": "margin",
    "通過": "passing_order",
    "上り": "last_3f",
    "上がり": "last_3f",
    "馬体重": "horse_weight",
    "賞金": "prize_money",
}
_COAT_COLORS = (
    "黒鹿毛",
    "青鹿毛",
    "尾花栗毛",
    "栃栗毛",
    "青毛",
    "芦毛",
    "白毛",
    "栗毛",
    "鹿毛",
    "月毛",
)


@dataclass
class _HorseIdentity:
    """Fields read from a horse profile or result-page header."""

    horse_name: str | None = None
    english_name: str | None = None
    sex: str | None = None
    coat_color: str | None = None
    birth_date: date | None = None
    birth_year: int | None = None
    sire_name: str | None = None
    dam_name: str | None = None
    damsire_name: str | None = None
    breeder_name: str | None = None
    breeder_location: str | None = None
    owner_name: str | None = None
    trainer_name: str | None = None
    training_center: str | None = None
    total_races: int | None = None
    total_wins: int | None = None
    total_earnings: int | None = None

    def fill_missing(self, fallback: "_HorseIdentity") -> "_HorseIdentity":
        """Keep values already found and fill only the blanks."""
        merged: dict[str, object] = {}
        for field in fields(self):
            current = getattr(self, field.name)
            other = getattr(fallback, field.name)
            merged[field.name] = current if current is not None else other
        return _HorseIdentity(**merged)  # type: ignore[arg-type]


def _compact(text: str) -> str:
    """Drop whitespace so ``天<br>気`` and ``天気`` share a header name."""
    return re.sub(r"\s+", "", text)


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.split())


def _optional_text(text: str) -> str | None:
    cleaned = _clean(text)
    if not cleaned or cleaned in _PLACEHOLDERS:
        return None
    return cleaned


def _optional_int(text: str) -> int | None:
    compact = _compact(text)
    if re.fullmatch(r"\d+", compact):
        return int(compact)
    return None


def _optional_float(text: str) -> float | None:
    compact = _compact(text).replace(",", "").replace("，", "")
    if not compact or compact in _PLACEHOLDERS:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", compact):
        return float(compact)
    return None


def _parse_earnings_man(text: str) -> int | None:
    """Parse a 獲得賞金 cell into 万円. ``17億5,347万円`` is 175347."""
    compact = _compact(text).replace(",", "").replace("，", "")
    if not compact or compact in _PLACEHOLDERS:
        return None
    match = re.fullmatch(r"(?:(\d+)億)?(?:(\d+)万)?円", compact)
    if match is None or (match.group(1) is None and match.group(2) is None):
        return None
    return int(match.group(1) or 0) * 10_000 + int(match.group(2) or 0)


def _parse_career(text: str) -> tuple[int | None, int | None]:
    """Read ``16戦8勝``. The bracket record is not turned into extra wins."""
    match = re.search(r"(\d+)\s*戦\s*(\d+)\s*勝", text)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _parse_distance_cell(text: str) -> tuple[int | None, str | None]:
    compact = _compact(text)
    if not compact or compact in _PLACEHOLDERS:
        return None, None
    match = re.search(r"(芝|ダート|ダ|障).*?(\d{3,4})", compact)
    if match:
        surface, distance = match.group(1), int(match.group(2))
        if surface == "芝":
            track_type = "芝"
        elif surface == "障":
            track_type = "障害"
        else:
            track_type = "ダート"
        return distance, track_type
    number = re.fullmatch(r"(\d{3,4})m?", compact)
    if number:
        return int(number.group(1)), None
    return None, None


def _parse_going(text: str) -> str | None:
    compact = _compact(text)
    if not compact or compact in _PLACEHOLDERS:
        return None
    for condition in ("稍重", "不良", "良", "重"):
        if condition in compact:
            return condition
    if compact == "稍":
        return "稍重"
    if compact == "不":
        return "不良"
    return None


def _parse_finish_time(text: str) -> str | None:
    cleaned = _clean(text)
    if not cleaned or cleaned in _PLACEHOLDERS:
        return None
    if re.fullmatch(r"\d+:\d+\.\d+", cleaned) or re.fullmatch(r"\d+\.\d+", cleaned):
        return cleaned
    return None


def _parse_impost(text: str) -> float | None:
    compact = _compact(text)
    if not compact or compact in _PLACEHOLDERS:
        return None
    match = re.fullmatch(r"[☆△▲★◇]*(\d+(?:\.\d+)?)", compact)
    if not match:
        return None
    return float(match.group(1))


def _parse_horse_weight(text: str) -> tuple[int | None, int | None]:
    compact = _compact(text)
    match = re.fullmatch(r"(\d+)\(([+-]?\d+)\)", compact)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _parse_trainer(text: str) -> tuple[str | None, str | None]:
    cleaned = _clean(text)
    if not cleaned or cleaned in _PLACEHOLDERS:
        return None, None
    match = re.match(r"(.+?)\s*[（(]\s*(美浦|栗東)\s*[）)]\s*$", cleaned)
    if match:
        return match.group(1).strip(), match.group(2)
    center = None
    if "美浦" in cleaned or "(美)" in cleaned:
        center = "美浦"
    elif "栗東" in cleaned or "(栗)" in cleaned:
        center = "栗東"
    return cleaned, center


def _normalize_sex(text: str) -> str | None:
    compact = _compact(text)
    if "牡" in compact:
        return "牡"
    if "牝" in compact:
        return "牝"
    if "騸" in compact or "セ" in compact:
        return "セ"
    return None


def _header_label(text: str) -> str:
    return _compact(text)


def _profile_label(text: str) -> str:
    return _compact(text).replace("（", "(").replace("）", ")")


def _cell(cols: list[Tag], columns: dict[str, int], field: str) -> Tag | None:
    index = columns.get(field)
    if index is None or index >= len(cols):
        return None
    return cols[index]


def _cell_text(cols: list[Tag], columns: dict[str, int], field: str) -> str:
    cell = _cell(cols, columns, field)
    if cell is None:
        return ""
    return cell.get_text()


def _race_id_from_cell(cell: Tag | None) -> str | None:
    if cell is None:
        return None
    for link in cell.find_all("a", href=True):
        match = re.search(r"/race/(\d+)", str(link.get("href", "")))
        if match:
            return match.group(1)
    return None


class NetkeibaScraper(BaseScraper):
    """Scraper for netkeiba website (https://race.netkeiba.com)."""

    BASE_URL = "https://race.netkeiba.com"
    DB_URL = "https://db.netkeiba.com"
    ENCODING = "euc-jp"
    # Same browser UA as the odds client. Accept stays HTML because this is a page.
    BROWSER_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }

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

    def get_races_by_date(self, target_date: date) -> list[Race]:
        """Get all races held on a specific date.

        Args:
            target_date: Race date to fetch

        Returns:
            Races found on the netkeiba race list for that date
        """
        date_str = target_date.strftime("%Y%m%d")
        races: list[Race] = []

        try:
            soup = self.fetch_page(
                f"{self.BASE_URL}/top/race_list.html",
                params={"kaisai_date": date_str},
            )
            containers = soup.find_all(
                ["div", "table", "section"],
                class_=re.compile(r"RaceList|race_list|RaceData"),
            )
            search_roots: list[Tag] = containers or [soup]
            seen_ids: set[str] = set()

            for root in search_roots:
                for link in root.find_all("a", href=re.compile(r"race_id=\d+")):
                    if not isinstance(link, Tag):
                        continue
                    race_id = self._extract_race_id_from_url(str(link.get("href", "")))
                    if not race_id or race_id in seen_ids:
                        continue
                    seen_ids.add(race_id)
                    race = self._parse_race_link(link, race_id)
                    if not race:
                        continue
                    venue_code, venue_name = self._resolve_venue(link, race_id)
                    races.append(
                        race.model_copy(
                            update={
                                "race_date": target_date,
                                "venue": venue_name,
                                "venue_code": venue_code,
                            }
                        )
                    )
        except Exception as e:
            logger.error(f"Failed to fetch races for {target_date}: {e}")

        unique_races = {race.race_id: race for race in races}
        return sorted(
            unique_races.values(),
            key=lambda race: (race.venue, race.race_number, race.race_id),
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

    def get_horse_info(
        self, horse_id: str, *, fetched_at: datetime | None = None
    ) -> Horse | None:
        """Get horse identity and near-run history.

        Performances come from ``/horse/result/{horse_id}/``.
        Name and 通算成績 come from ``/horse/{horse_id}/`` when that page has them,
        with the result-page header as a fallback for the name.
        This is past-race history for near-run evaluation, not the result of the
        race being predicted. Missing cells stay null. A failed or empty result
        page returns no rows and sets ``warning``.

        Args:
            horse_id: Horse ID
            fetched_at: Timestamp stored on the horse. Defaults to now.

        Returns:
            Horse information, or None when neither page identifies the horse
        """
        fetched = fetched_at or datetime.now()
        profile_url = f"{self.DB_URL}/horse/{horse_id}/"
        result_url = f"{self.DB_URL}/horse/result/{horse_id}/"
        profile_soup: BeautifulSoup | None = None
        result_soup: BeautifulSoup | None = None
        result_error = False

        try:
            profile_soup = self._fetch_horse_page(profile_url, referer=f"{self.DB_URL}/")
        except Exception as e:
            logger.error("Failed to fetch horse profile for %s: %s", horse_id, e)

        try:
            result_soup = self._fetch_horse_page(result_url, referer=profile_url)
        except Exception as e:
            logger.error("Failed to fetch horse performances for %s: %s", horse_id, e)
            result_error = True

        if profile_soup is None and result_soup is None:
            return None

        if result_error or result_soup is None:
            performances: list[HorsePerformance] = []
            warning: str | None = HORSE_RESULT_FETCH_FAILED
        else:
            performances, warning = self._parse_horse_performances(result_soup)

        identity = (
            self._parse_horse_identity(profile_soup)
            if profile_soup is not None
            else _HorseIdentity()
        )
        if result_soup is not None:
            identity = identity.fill_missing(self._parse_horse_identity(result_soup))

        if identity.horse_name is None and identity.total_races is None and not performances:
            return None

        return Horse(
            horse_id=horse_id,
            horse_name=identity.horse_name or "不明",
            english_name=identity.english_name,
            sex=identity.sex or "不明",
            birth_date=identity.birth_date,
            birth_year=identity.birth_year,
            coat_color=identity.coat_color,
            sire_name=identity.sire_name,
            dam_name=identity.dam_name,
            damsire_name=identity.damsire_name,
            breeder_name=identity.breeder_name,
            breeder_location=identity.breeder_location,
            owner_name=identity.owner_name,
            trainer_name=identity.trainer_name,
            training_center=identity.training_center,
            total_races=identity.total_races,
            total_wins=identity.total_wins,
            total_earnings=identity.total_earnings,
            performances=performances,
            fetched_at=fetched,
            warning=warning,
        )

    def get_horse_performances(
        self, horse_id: str, limit: int = 10
    ) -> list[HorsePerformance]:
        """Get a horse's past performances from the result page.

        Args:
            horse_id: Horse ID
            limit: Maximum number of performances to return

        Returns:
            Past performances, or an empty list when the page cannot be read.
            Rows are not invented. Callers that need the reason should use
            ``get_horse_info``, which sets ``warning``.
        """
        url = f"{self.DB_URL}/horse/result/{horse_id}/"
        try:
            soup = self._fetch_horse_page(
                url,
                referer=f"{self.DB_URL}/horse/{horse_id}/",
            )
        except Exception as e:
            logger.error("Failed to fetch horse performances for %s: %s", horse_id, e)
            return []

        performances, _warning = self._parse_horse_performances(soup, limit)
        return performances

    def _fetch_horse_page(self, url: str, *, referer: str) -> BeautifulSoup:
        """Fetch a db.netkeiba horse page with the odds client's browser UA."""
        headers = {**self.BROWSER_HEADERS, "Referer": referer}
        return self.fetch_page(url, headers=headers)

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

    def _parse_horse_identity(self, soup: BeautifulSoup) -> _HorseIdentity:
        """Read name, profile, and 通算成績. Does not count result rows."""
        identity = _HorseIdentity(
            horse_name=self._horse_name(soup),
            english_name=self._english_name(soup),
        )
        identity.sex, identity.coat_color = self._sex_and_coat(soup)
        profile = self._profile_table(soup)
        table_sex = _normalize_sex(profile.get("性別", ""))
        if table_sex:
            identity.sex = table_sex
        birth = parse_japanese_date(profile.get("生年月日", ""))
        if birth:
            identity.birth_date = birth
            identity.birth_year = birth.year
        identity.trainer_name, identity.training_center = _parse_trainer(
            profile.get("調教師", "")
        )
        identity.owner_name = _optional_text(profile.get("馬主", ""))
        identity.breeder_name = _optional_text(profile.get("生産者", ""))
        identity.breeder_location = _optional_text(profile.get("産地", ""))
        identity.sire_name = _optional_text(profile.get("父", ""))
        identity.dam_name = _optional_text(profile.get("母", ""))
        identity.damsire_name = _optional_text(profile.get("母父", ""))
        self._fill_blood(soup, identity)
        identity.total_races, identity.total_wins = _parse_career(profile.get("通算成績", ""))
        central = _parse_earnings_man(profile.get("獲得賞金(中央)", ""))
        local = _parse_earnings_man(profile.get("獲得賞金(地方)", ""))
        if central is None and local is None:
            identity.total_earnings = None
        elif central is None:
            identity.total_earnings = local
        elif local is None:
            identity.total_earnings = central
        else:
            identity.total_earnings = central + local
        return identity

    def _horse_name(self, soup: BeautifulSoup) -> str | None:
        for node in soup.select(".horse_title h1"):
            name = _optional_text(node.get_text())
            if name:
                return name
        legacy = soup.find(class_=re.compile(r"horse_name|HorseName"))
        if isinstance(legacy, Tag):
            name = _optional_text(legacy.get_text())
            if name:
                return name
        for node in soup.find_all("h1"):
            name = _optional_text(node.get_text())
            if name:
                return name
        return None

    def _english_name(self, soup: BeautifulSoup) -> str | None:
        node = soup.select_one(".horse_title .eng_name")
        if node is None:
            return None
        return _optional_text(node.get_text())

    def _sex_and_coat(self, soup: BeautifulSoup) -> tuple[str | None, str | None]:
        node = soup.select_one(".horse_title .txt_01")
        compact = _compact(node.get_text()) if node else ""
        coat = next((color for color in _COAT_COLORS if color in compact), None)
        return _normalize_sex(compact), coat

    def _profile_table(self, soup: BeautifulSoup) -> dict[str, str]:
        table = soup.find("table", class_=re.compile(r"db_prof_table|profile"))
        data: dict[str, str] = {}
        if not isinstance(table, Tag):
            return data
        for row in table.find_all("tr"):
            header = row.find("th")
            value = row.find("td")
            if isinstance(header, Tag) and isinstance(value, Tag):
                data[_profile_label(header.get_text())] = value.get_text()
        return data

    def _fill_blood(self, soup: BeautifulSoup, identity: _HorseIdentity) -> None:
        if identity.sire_name:
            return
        blood = soup.find("table", class_=re.compile(r"blood"))
        if not isinstance(blood, Tag):
            return
        links = blood.find_all("a")
        if len(links) >= 1:
            identity.sire_name = _optional_text(links[0].get_text())
        if len(links) >= 2 and not identity.dam_name:
            identity.dam_name = _optional_text(links[1].get_text())

    def _parse_horse_performances(
        self, soup: BeautifulSoup, limit: int | None = None
    ) -> tuple[list[HorsePerformance], str | None]:
        """Parse ``/horse/result/{id}/`` by header name, not column index.

        The live table inserts 水分量 before 馬場 and pace/index columns before 上り.
        Fixed indexes read those cells as going, time, and last 3F.
        """
        table = self._find_result_table(soup)
        if table is None:
            return [], HORSE_RESULT_TABLE_NOT_FOUND
        columns = self._result_columns(table)
        if "date" not in columns or "rank" not in columns:
            return [], HORSE_RESULT_TABLE_MALFORMED
        performances: list[HorsePerformance] = []
        for row in table.find_all("tr"):
            cols = [cell for cell in row.find_all("td", recursive=False) if isinstance(cell, Tag)]
            if not cols:
                continue
            try:
                perf = self._parse_performance_row(cols, columns)
            except Exception as e:
                logger.warning("Failed to parse performance row: %s", e)
                continue
            if perf is not None:
                performances.append(perf)
        if not performances:
            return [], HORSE_RESULT_EMPTY
        if limit is not None:
            performances = performances[:limit]
        return performances, None

    def _find_result_table(self, soup: BeautifulSoup) -> Tag | None:
        table = soup.find("table", class_="db_h_race_results")
        if isinstance(table, Tag):
            return table
        fallback = soup.find("table", class_=re.compile(r"nk_tb_common|db_h_race"))
        if isinstance(fallback, Tag):
            return fallback
        return None

    def _result_columns(self, table: Tag) -> dict[str, int]:
        for row in table.find_all("tr"):
            cells = [
                cell
                for cell in row.find_all(["th", "td"], recursive=False)
                if isinstance(cell, Tag)
            ]
            labels = [_header_label(cell.get_text()) for cell in cells]
            if "日付" not in labels or "着順" not in labels:
                continue
            columns: dict[str, int] = {}
            for index, label in enumerate(labels):
                field = _RESULT_HEADERS.get(label)
                if field and field not in columns:
                    columns[field] = index
            return columns
        return {}

    def _parse_performance_row(
        self, cols: list[Tag], columns: dict[str, int]
    ) -> HorsePerformance | None:
        """Parse one result row. Unreadable cells stay null."""
        date_cell = _cell(cols, columns, "date")
        if date_cell is None:
            return None
        race_date = parse_japanese_date(_clean(date_cell.get_text()))
        if race_date is None:
            return None

        distance, track_type = _parse_distance_cell(_cell_text(cols, columns, "distance"))
        horse_weight, horse_weight_diff = _parse_horse_weight(
            _cell_text(cols, columns, "horse_weight")
        )
        return HorsePerformance(
            race_date=race_date,
            race_id=_race_id_from_cell(_cell(cols, columns, "race_name")),
            race_name=_optional_text(_cell_text(cols, columns, "race_name")) or "",
            venue=_optional_text(_cell_text(cols, columns, "venue")) or "",
            distance=distance,
            track_type=track_type,
            track_condition=_parse_going(_cell_text(cols, columns, "track_condition")),
            weather=_optional_text(_cell_text(cols, columns, "weather")),
            entry_count=_optional_int(_cell_text(cols, columns, "entry_count")),
            horse_number=_optional_int(_cell_text(cols, columns, "horse_number")),
            rank=_optional_int(_cell_text(cols, columns, "rank")),
            odds=_optional_float(_cell_text(cols, columns, "odds")),
            popularity=_optional_int(_cell_text(cols, columns, "popularity")),
            jockey_name=_optional_text(_cell_text(cols, columns, "jockey")),
            weight=_parse_impost(_cell_text(cols, columns, "weight")),
            horse_weight=horse_weight,
            horse_weight_diff=horse_weight_diff,
            finish_time=_parse_finish_time(_cell_text(cols, columns, "finish_time")),
            margin=_optional_text(_cell_text(cols, columns, "margin")),
            passing_order=_optional_text(_cell_text(cols, columns, "passing_order")),
            last_3f=_optional_float(_cell_text(cols, columns, "last_3f")),
            prize_money=_optional_float(_cell_text(cols, columns, "prize_money")),
        )


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

    def _resolve_venue(self, link: Tag, race_id: str) -> tuple[str, str]:
        """Resolve venue code and name from link text or race ID.

        Netkeiba race IDs are YYYY + venue + kai + day + race.
        IDs generated in this project are YYYYMMDD + venue + race.
        """
        text = self.clean_text(link.get_text())
        for code, name in self.VENUE_CODES.items():
            if name in text:
                return code, name

        if len(race_id) >= 12 and race_id.isdigit():
            netkeiba_venue = race_id[4:6]
            kai = int(race_id[6:8])
            day_number = int(race_id[8:10])
            if (
                netkeiba_venue in self.VENUE_CODES
                and 1 <= kai <= 6
                and 1 <= day_number <= 12
            ):
                return netkeiba_venue, self.VENUE_CODES[netkeiba_venue]

            project_venue = race_id[8:10]
            if project_venue in self.VENUE_CODES:
                return project_venue, self.VENUE_CODES[project_venue]

        code = self._extract_venue_code_from_race_id(race_id)
        return code, self.VENUE_CODES.get(code, "不明")

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
