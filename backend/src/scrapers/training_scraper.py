"""Training clocks from 競馬ブック and speedkeiba.

競馬ブックの調教は ``https://p.keibabook.co.jp/cyuou/cyokyo/0/0/{race_id}``。
列名（5F、半哩、坂路の 4F）が HTML にある。Smart の ``s.keibabook.co.jp`` は
同じ表だが列名が無いので、位置からハロンは作らない。

匿名の応答に載っている馬だけを埋める。載っていない馬は null と、
ログインが要るという warning。馬個別の調教履歴 ``/db/uma/{umacd}/crireki`` は
ログイン無しでは本文が空で、netkeiba の馬ID とは別の番号なので取りにいかない。

speedkeiba は今週の重賞記事。時計の行と、ページが印刷した ``(平均)`` と
``今走`` のラップだけを使う。``好調`` と ``凡走`` はページ自身が
人気・着順・着差から作った行なのでコピーしない。
"""

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, Tag

from backend.models.training import (
    SOURCE_KEIBABOOK_SMART,
    SOURCE_SPEEDKEIBA,
    TRAINING_AMBIGUOUS,
    TRAINING_FETCH_FAILED,
    TRAINING_HORSE_UNAVAILABLE,
    TRAINING_NOT_ON_EITHER,
    TRAINING_NOT_ON_KEIBABOOK,
    TRAINING_TABLE_MALFORMED,
    TRAINING_TABLE_NOT_FOUND,
    PublishedAverage,
    Training,
    TrainingLap,
    TrainingSplit,
    TrainingSummary,
    Workout,
    build_training,
    unavailable_training,
)
from backend.scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

KEIBABOOK_CYOKYO = "https://p.keibabook.co.jp/cyuou/cyokyo/0/0/{race_id}"
SPEEDKEIBA_ORIGIN = "https://speedkeiba.com"
SPEEDKEIBA_INDEX = "https://speedkeiba.com/%E4%BB%8A%E9%80%B1%E3%81%AE%E3%83%AC%E3%83%BC%E3%82%B9/"
_MAX_ARTICLES = 12

_RACE_ID = re.compile(r"^\d{12}$")
_RACE_DATE = re.compile(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日")
_MONTH_DAY = re.compile(r"^[*＊]?\s*(\d{1,2})/(\d{1,2})")
_REPS = re.compile(r"^(\d+)回$")
_CLOCK = re.compile(r"^\d+(?:\.\d+)?$")
_CLOCK_ROW = re.compile(
    r"^(?:\d+(?:\.\d+)?)(?:\s*[–—\-]\s*(?:\d+(?:\.\d+)?))*$"
)
_RANK = re.compile(r"^[［\[]\s*(\d+)\s*[］\]]$")
_YEAR = re.compile(r"20\d{2}")
_KAI = re.compile(r"^第\d+回")

_FLAT_OR_SLOPE = (
    ("6F(坂路)", "6F", "reps"),
    ("5F(4F)", "5F", "4F"),
    ("半哩(3F)", "半哩", "3F"),
    ("3F(2F)", "3F", "2F"),
    ("1F(1F)", "1F", "1F"),
)


@dataclass
class TrainingPage:
    """Horses parsed from one HTML document. ``page_warning`` is page-level."""

    by_name: dict[str, Training] = field(default_factory=dict)
    page_warning: str | None = None


def name_key(name: str) -> str:
    """Match 馬名 without spaces. Do not strip a prefix or invent an alias."""
    text = unicodedata.normalize("NFKC", name)
    return re.sub(r"\s+", "", text).casefold()


def parse_keibabook_cyokyo(html: str) -> TrainingPage:
    """Read every horse table on a 競馬ブック調教 page."""
    soup = BeautifulSoup(html, "lxml")
    tables = [
        table
        for table in soup.find_all("table")
        if isinstance(table, Tag) and "cyokyo" in (table.get("class") or [])
    ]
    if not tables:
        return TrainingPage(page_warning=TRAINING_TABLE_NOT_FOUND)
    race_day = _race_day(soup)
    by_name: dict[str, Training] = {}
    for table in tables:
        for name, training in _read_keibabook_table(table, race_day):
            by_name[name_key(name)] = training
    if not by_name:
        return TrainingPage(page_warning=TRAINING_TABLE_MALFORMED)
    return TrainingPage(by_name=by_name)


def parse_speedkeiba_article(html: str) -> TrainingPage:
    """Read one speedkeiba race article. 好調 and 凡走 rows are dropped."""
    soup = BeautifulSoup(html, "lxml")
    year = _article_year(soup)
    headings = [tag for tag in soup.find_all("h4") if isinstance(tag, Tag)]
    if not headings:
        return TrainingPage(page_warning=TRAINING_TABLE_NOT_FOUND)
    by_name: dict[str, Training] = {}
    for heading in headings:
        name = heading.get_text(" ", strip=True)
        if not name:
            continue
        by_name[name_key(name)] = _read_speedkeiba_horse(heading, year)
    if not by_name:
        return TrainingPage(page_warning=TRAINING_TABLE_MALFORMED)
    return TrainingPage(by_name=by_name)


def parse_speedkeiba_index(html: str) -> list[tuple[str, str]]:
    """``(absolute url, link text)`` for this week's 調教 articles."""
    soup = BeautifulSoup(html, "lxml")
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for heading in soup.find_all("h3"):
        if not isinstance(heading, Tag):
            continue
        anchor = heading.find("a", href=True)
        if not isinstance(anchor, Tag):
            continue
        title = anchor.get_text(" ", strip=True)
        href = str(anchor.get("href") or "")
        if "調教" not in title or not href:
            continue
        url = urljoin(SPEEDKEIBA_ORIGIN + "/", href)
        if url in seen:
            continue
        seen.add(url)
        found.append((url, title))
    return found


def match_speedkeiba_article(race_name: str, articles: list[tuple[str, str]]) -> str | None:
    """One article whose title is this race. A partial or double match is no match."""
    key = _race_key(race_name)
    if len(key) < 4:
        return None
    hits: list[str] = []
    for url, title in articles:
        if _race_key(title) == key:
            hits.append(url)
    unique = list(dict.fromkeys(hits))
    if len(unique) == 1:
        return unique[0]
    return None


class TrainingScraper(BaseScraper):
    """Fetch 調教 HTML and return only clocks that were on the page."""

    ENCODING = "utf-8"

    def training_for_horse(self, horse_name: str) -> Training:
        """speedkeiba weekly articles, matched on the h4 horse name.

        競馬ブックの馬個別履歴はログインが要り、馬IDも netkeiba と違う。
        記事が無ければその旨の warning。時計は作らない。
        """
        if not horse_name or horse_name == "不明":
            return unavailable_training(TRAINING_HORSE_UNAVAILABLE)
        try:
            index_html = self.http_client.get(SPEEDKEIBA_INDEX, encoding=self.ENCODING)
        except Exception as e:
            logger.error("Failed to fetch speedkeiba index: %s", e)
            return unavailable_training(TRAINING_FETCH_FAILED)
        articles = parse_speedkeiba_index(index_html)[:_MAX_ARTICLES]
        if not articles:
            return unavailable_training(TRAINING_HORSE_UNAVAILABLE)

        matches: list[Training] = []
        malformed: Training | None = None
        fetched = False
        for url, _title in articles:
            try:
                html = self.http_client.get(url, encoding=self.ENCODING)
            except Exception as e:
                logger.error("Failed to fetch speedkeiba article %s: %s", url, e)
                continue
            fetched = True
            page = parse_speedkeiba_article(html)
            found = page.by_name.get(name_key(horse_name))
            if found is None:
                continue
            if found.warning == TRAINING_TABLE_MALFORMED:
                malformed = found
                continue
            if any(row.time for row in found.workouts) or found.summary is not None:
                matches.append(found)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return unavailable_training(TRAINING_AMBIGUOUS)
        if malformed is not None:
            return malformed
        if not fetched:
            return unavailable_training(TRAINING_FETCH_FAILED)
        return unavailable_training(TRAINING_HORSE_UNAVAILABLE)

    def training_for_race(
        self, race_id: str, race_name: str, horse_names: list[str]
    ) -> dict[str, Training]:
        """Keibabook clocks when that horse is on the page, else a speedkeiba article."""
        keibabook, kb_warning = self._load_keibabook(race_id)
        speed, speed_checked = self._load_speed_for_race(race_name)
        chosen: dict[str, Training] = {}
        for horse_name in horse_names:
            chosen[horse_name] = _choose_training(
                horse_name,
                keibabook,
                kb_warning,
                speed,
                speed_checked,
            )
        return chosen

    def _load_keibabook(self, race_id: str) -> tuple[dict[str, Training], str | None]:
        if _RACE_ID.fullmatch(race_id) is None:
            return {}, TRAINING_NOT_ON_KEIBABOOK
        url = KEIBABOOK_CYOKYO.format(race_id=race_id)
        try:
            html = self.http_client.get(url, encoding=self.ENCODING)
        except Exception as e:
            logger.error("Failed to fetch keibabook training for %s: %s", race_id, e)
            return {}, TRAINING_FETCH_FAILED
        page = parse_keibabook_cyokyo(html)
        if page.page_warning:
            return {}, page.page_warning
        return page.by_name, None

    def _load_speed_for_race(self, race_name: str) -> tuple[dict[str, Training], bool]:
        """Return parsed horses and whether an article for this race was opened."""
        try:
            index_html = self.http_client.get(SPEEDKEIBA_INDEX, encoding=self.ENCODING)
        except Exception as e:
            logger.error("Failed to fetch speedkeiba index: %s", e)
            return {}, False
        url = match_speedkeiba_article(race_name, parse_speedkeiba_index(index_html))
        if url is None:
            return {}, False
        try:
            html = self.http_client.get(url, encoding=self.ENCODING)
        except Exception as e:
            logger.error("Failed to fetch speedkeiba article %s: %s", url, e)
            return {}, False
        page = parse_speedkeiba_article(html)
        if page.page_warning:
            return {}, True
        return page.by_name, True


def _choose_training(
    horse_name: str,
    keibabook: dict[str, Training],
    kb_warning: str | None,
    speed: dict[str, Training],
    speed_checked: bool,
) -> Training:
    kb = keibabook.get(name_key(horse_name))
    sp = speed.get(name_key(horse_name))
    if _has_time(kb):
        return kb  # type: ignore[return-value]
    if _has_time(sp):
        return sp  # type: ignore[return-value]
    if kb is not None and kb.warning == TRAINING_TABLE_MALFORMED:
        return kb
    if kb is not None and kb.summary is not None and kb.warning != TRAINING_TABLE_MALFORMED:
        return kb
    if sp is not None and sp.summary is not None:
        return sp
    if kb_warning == TRAINING_FETCH_FAILED and not speed_checked:
        return unavailable_training(TRAINING_FETCH_FAILED)
    if kb_warning == TRAINING_TABLE_MALFORMED and not _has_time(sp):
        return unavailable_training(TRAINING_TABLE_MALFORMED)
    if speed_checked:
        return unavailable_training(TRAINING_NOT_ON_EITHER)
    if kb_warning == TRAINING_TABLE_NOT_FOUND:
        return unavailable_training(TRAINING_TABLE_NOT_FOUND)
    return unavailable_training(kb_warning or TRAINING_NOT_ON_KEIBABOOK)


def _has_time(training: Training | None) -> bool:
    if training is None or training.warning == TRAINING_TABLE_MALFORMED:
        return False
    return any(row.time for row in training.workouts)


def _read_keibabook_table(table: Tag, race_day: date | None) -> list[tuple[str, Training]]:
    horses: list[tuple[str, Training]] = []
    current_name: str | None = None
    current_summary = TrainingSummary()
    workouts: list[Workout] = []
    malformed = False

    def flush() -> None:
        nonlocal current_name, current_summary, workouts, malformed
        if current_name is None:
            return
        warning = TRAINING_TABLE_MALFORMED if malformed else None
        kept = [] if malformed else workouts
        summary = current_summary
        horses.append(
            (
                current_name,
                build_training(
                    workouts=kept,
                    summary=summary,
                    source=SOURCE_KEIBABOOK_SMART,
                    warning=warning,
                ),
            )
        )
        current_name = None
        current_summary = TrainingSummary()
        workouts = []
        malformed = False

    for row in table.find_all("tr"):
        if not isinstance(row, Tag) or row.find_parent("table") is not table:
            continue
        name_cell = _direct_cell(row, "kbamei")
        if name_cell is not None:
            flush()
            current_name = name_cell.get_text(" ", strip=True) or None
            comment = _direct_text(row, "tanpyo")
            arrow = _direct_text(row, "yajirusi")
            current_summary = TrainingSummary(comment=comment, arrow=arrow)
            continue
        if current_name is None:
            continue
        inner = row.find("table", class_="cyokyodata")
        if not isinstance(inner, Tag):
            continue
        parsed, bad = _read_cyokyodata(inner, race_day)
        if bad:
            malformed = True
            workouts = []
        elif not malformed:
            workouts.extend(parsed)
    flush()
    return horses


def _read_cyokyodata(
    table: Tag, race_day: date | None
) -> tuple[list[Workout], bool]:
    headers = _header_cells(table)
    columns = _column_index(headers)
    if columns is None:
        return [], True
    workouts: list[Workout] = []
    for row in table.find_all("tr"):
        if not isinstance(row, Tag) or row.find_parent("table") is not table:
            continue
        classes = set(row.get("class") or [])
        if "awase" in classes:
            note = row.get_text(" ", strip=True) or None
            if workouts and note:
                workouts[-1] = workouts[-1].model_copy(update={"partner_note": note})
            continue
        if "time" not in classes and "oikiri" not in classes:
            continue
        cells = [cell for cell in row.find_all("td", recursive=False) if isinstance(cell, Tag)]
        if len(cells) != len(headers):
            return [], True
        workout = _workout_from_cells(cells, columns, race_day, "oikiri" in classes)
        if workout is None:
            return [], True
        workouts.append(workout)
    return workouts, False


def _workout_from_cells(
    cells: list[Tag],
    columns: dict[str, int | list[tuple[int, str]]],
    race_day: date | None,
    oikiri: bool,
) -> Workout | None:
    def text_at(key: str) -> str:
        index = columns[key]
        if not isinstance(index, int) or index < 0:
            return ""
        return cells[index].get_text(" ", strip=True)

    date_label = text_at("date") or None
    course = text_at("course") or None
    condition = text_at("condition") or None
    rider = text_at("rider") or None
    gait = text_at("gait") or None
    comment = text_at("comment") or None
    mark = text_at("mark") or None
    slope = bool(course and "坂" in course)
    time_columns = columns["times"]
    if not isinstance(time_columns, list):
        return None
    if not slope:
        for index, _label in time_columns:
            if _REPS.fullmatch(cells[index].get_text(" ", strip=True)):
                slope = True
                break

    splits: list[TrainingSplit] = []
    slope_reps: int | None = None
    for index, header in time_columns:
        raw = cells[index].get_text(" ", strip=True)
        if not raw:
            continue
        label = _split_label(header, slope)
        reps = _REPS.fullmatch(raw)
        if label == "reps" or reps:
            if reps is None:
                return None
            slope_reps = int(reps.group(1))
            continue
        if _CLOCK.fullmatch(raw) is None:
            return None
        if label is None:
            return None
        splits.append(TrainingSplit(label=label, time=raw))

    rank = _rank_in_group(text_at("rank"))
    parsed_date, long_gap = _resolve_date(date_label, race_day)
    distance = splits[0].label if splits else None
    clock = splits[0].time if splits else None
    final = True if oikiri or mark == "☆" else None
    return Workout(
        date=parsed_date,
        date_label=date_label,
        course=course,
        distance=distance,
        time=clock,
        condition=condition,
        rank_in_group=rank,
        rider=rider,
        gait=gait,
        comment=comment,
        mark=mark,
        final_workout=final,
        long_gap=long_gap,
        slope_reps=slope_reps,
        splits=splits,
    )


def _header_cells(table: Tag) -> list[str]:
    header_row = table.find("tr")
    if not isinstance(header_row, Tag):
        return []
    headers: list[str] = []
    for cell in header_row.find_all(["th", "td"], recursive=False):
        if isinstance(cell, Tag):
            headers.append(re.sub(r"\s+", "", cell.get_text()))
    return headers


def _column_index(
    headers: list[str],
) -> dict[str, int | list[tuple[int, str]]] | None:
    if not headers:
        return None
    found: dict[str, int] = {}
    times: list[tuple[int, str]] = []
    for index, header in enumerate(headers):
        if header in {"騎乗者"}:
            found["rider"] = index
        elif header in {"日付"}:
            found["date"] = index
        elif header in {"コース"}:
            found["course"] = index
        elif header.startswith("馬場") or header == "馬場":
            found["condition"] = index
        elif "回り" in header and "位置" in header:
            found["rank"] = index
        elif header in {"脚色"}:
            found["gait"] = index
        elif header in {"短評"}:
            found["comment"] = index
        elif header in {"動画"}:
            found["movie"] = index
        elif _is_time_header(header):
            times.append((index, header))
        elif index == 0 and header == "":
            found["mark"] = index
    if "mark" not in found and headers and headers[0] == "":
        found["mark"] = 0
    required = ("date", "course", "condition", "rank")
    if any(key not in found for key in required) or not times:
        return None
    found.setdefault("rider", -1)
    found.setdefault("gait", -1)
    found.setdefault("comment", -1)
    found.setdefault("mark", -1)
    return {
        "rider": found["rider"],
        "date": found["date"],
        "course": found["course"],
        "condition": found["condition"],
        "rank": found["rank"],
        "gait": found["gait"],
        "comment": found["comment"],
        "mark": found["mark"],
        "times": times,
    }


def _is_time_header(header: str) -> bool:
    if not header:
        return False
    base = header.split("(")[0]
    return base in {"1哩", "7F", "6F", "5F", "半哩", "3F", "1F"} or header in {
        "6F(坂路)",
        "5F(4F)",
        "半哩(3F)",
        "3F(2F)",
        "1F(1F)",
    }


def _split_label(header: str, slope: bool) -> str | None:
    """Use the header's own parenthetical for 坂路. Do not convert 半哩 to meters."""
    if slope:
        for raw, _flat, slope_label in _FLAT_OR_SLOPE:
            if header == raw:
                return slope_label
        if header.startswith("6F"):
            return "reps"
    base = header.split("(")[0]
    if base:
        return base
    return None


def _rank_in_group(text: str) -> int | None:
    match = _RANK.fullmatch(text)
    if match is None:
        return None
    return int(match.group(1))


def _resolve_date(label: str | None, race_day: date | None) -> tuple[date | None, bool | None]:
    """A full date only when the page has a race day to pin the year. ``■`` stays null."""
    if not label:
        return None, None
    match = _MONTH_DAY.match(label)
    if match is None or race_day is None:
        return None, None
    month = int(match.group(1))
    day = int(match.group(2))
    try:
        candidate = date(race_day.year, month, day)
    except ValueError:
        return None, None
    if candidate > race_day:
        try:
            candidate = date(race_day.year - 1, month, day)
        except ValueError:
            return None, None
    long_gap = label.startswith("*") or label.startswith("＊")
    return candidate, long_gap


def _race_day(soup: BeautifulSoup) -> date | None:
    match = _RACE_DATE.search(soup.get_text(" ", strip=True))
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _direct_cell(row: Tag, class_name: str) -> Tag | None:
    for cell in row.find_all("td", recursive=False):
        if isinstance(cell, Tag) and class_name in (cell.get("class") or []):
            return cell
    return None


def _direct_text(row: Tag, class_name: str) -> str | None:
    cell = _direct_cell(row, class_name)
    if cell is None:
        return None
    text = cell.get_text(" ", strip=True)
    return text or None


def _read_speedkeiba_horse(heading: Tag, year: int | None) -> Training:
    workouts: list[Workout] = []
    section: str | None = None
    summary = TrainingSummary()
    malformed = False
    for node in _until_next_h4(heading):
        if not isinstance(node, Tag):
            continue
        if node.name == "h5":
            text = node.get_text(" ", strip=True)
            if "前回連対時" in text:
                section = "前回連対時"
            elif "今回" in text:
                section = "今回"
            continue
        classes = set(node.get("class") or [])
        if node.name == "div" and "flexbox" in classes:
            if "average" in classes:
                average = _published_average(node)
                if average is None:
                    malformed = True
                elif summary.average is None:
                    summary = summary.model_copy(update={"average": average})
                continue
            workout = _speed_workout(node, section, year)
            if workout == "bad":
                malformed = True
            elif isinstance(workout, Workout):
                workouts.append(workout)
            continue
        if node.name == "span" and "choukyou-evaluation" in classes:
            text = node.get_text(" ", strip=True) or None
            if text and summary.evaluation is None:
                summary = summary.model_copy(update={"evaluation": text})
            continue
        if node.name == "table" and "choukyou-laptime" in classes:
            lap = _thisweek_lap(node)
            if lap == "bad":
                malformed = True
            elif isinstance(lap, TrainingLap):
                summary = summary.model_copy(update={"laps": [*summary.laps, lap]})
    if malformed:
        return build_training(
            workouts=[],
            summary=None,
            source=SOURCE_SPEEDKEIBA,
            warning=TRAINING_TABLE_MALFORMED,
        )
    return build_training(
        workouts=workouts,
        summary=summary,
        source=SOURCE_SPEEDKEIBA,
        warning=None,
    )


def _until_next_h4(heading: Tag) -> list[Tag]:
    nodes: list[Tag] = []
    for sibling in heading.next_siblings:
        if isinstance(sibling, Tag) and sibling.name == "h4":
            break
        if isinstance(sibling, Tag):
            nodes.append(sibling)
            nodes.extend(tag for tag in sibling.find_all(True) if isinstance(tag, Tag))
    return nodes


def _published_average(node: Tag) -> PublishedAverage | None:
    label = _span_text(node, "date") or "(平均)"
    course = _basho_course(_span_text(node, "basho"))
    clock = _span_text(node, "time")
    if clock and _CLOCK_ROW.fullmatch(clock) is None:
        return None
    return PublishedAverage(label=label, course=course, time=clock)


def _speed_workout(node: Tag, section: str | None, year: int | None) -> Workout | str | None:
    label = _span_text(node, "date")
    basho = _span_text(node, "basho")
    clock = _span_text(node, "time")
    if clock and _CLOCK_ROW.fullmatch(clock) is None and not re.fullmatch(r"[–—\-\s]+", clock):
        return "bad"
    if not clock or re.fullmatch(r"[–—\-\s]+", clock):
        return None
    course, condition = _split_basho(basho)
    parsed: date | None = None
    long_gap: bool | None = None
    if section == "今回" and year is not None and label:
        match = _MONTH_DAY.match(label)
        if match:
            try:
                parsed = date(year, int(match.group(1)), int(match.group(2)))
                long_gap = False
            except ValueError:
                parsed = None
    return Workout(
        date=parsed,
        date_label=label,
        course=course,
        distance=None,
        time=clock,
        condition=condition,
        section=section,
        long_gap=long_gap,
    )


def _thisweek_lap(table: Tag) -> TrainingLap | str | None:
    header_row = table.find("tr")
    if not isinstance(header_row, Tag):
        return "bad"
    headers = [
        re.sub(r"\s+", "", cell.get_text())
        for cell in header_row.find_all(["th", "td"], recursive=False)
        if isinstance(cell, Tag)
    ]
    labels = headers[1:]
    if not labels or labels[-1] != "1F" or "4F" not in labels:
        return "bad"
    phase, course = _lap_caption(table)
    for row in table.find_all("tr"):
        if not isinstance(row, Tag) or row is header_row:
            continue
        cells = [
            cell.get_text(" ", strip=True)
            for cell in row.find_all(["td", "th"], recursive=False)
            if isinstance(cell, Tag)
        ]
        if not cells or cells[0] != "今走":
            continue
        if len(cells) != len(headers):
            return "bad"
        splits: list[TrainingSplit] = []
        for label, raw in zip(labels, cells[1:], strict=True):
            if not raw or raw in {"–", "—", "-"}:
                continue
            if _CLOCK.fullmatch(raw) is None:
                return "bad"
            splits.append(TrainingSplit(label=label, time=raw))
        if not splits:
            return None
        return TrainingLap(label="今走", phase=phase, course=course, splits=splits)
    return None


def _lap_caption(table: Tag) -> tuple[str | None, str | None]:
    for child in table.children:
        if not isinstance(child, Comment):
            continue
        match = re.search(r"caption>\s*([^<]+)", str(child))
        if match is None:
            continue
        text = match.group(1).strip()
        if "/" in text:
            course, phase = (part.strip() for part in text.split("/", 1))
            return phase or None, course or None
        return None, text or None
    return None, None


def _span_text(node: Tag, class_name: str) -> str | None:
    span = node.find("span", class_=class_name)
    if not isinstance(span, Tag):
        return None
    text = span.get_text(" ", strip=True)
    return text or None


def _split_basho(text: str | None) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    match = re.fullmatch(r"(.+?)\s*\(([^)]+)\)\s*", text)
    if match is None:
        return text.strip() or None, None
    return match.group(1).strip() or None, match.group(2).strip() or None


def _basho_course(text: str | None) -> str | None:
    course, _condition = _split_basho(text)
    return course


def _article_year(soup: BeautifulSoup) -> int | None:
    for tag in soup.find_all(["h2", "title"]):
        match = _YEAR.search(tag.get_text(" ", strip=True))
        if match:
            return int(match.group(0))
    return None


def _race_key(name: str) -> str:
    text = unicodedata.normalize("NFKC", name)
    text = re.sub(r"\s+", "", text)
    text = _KAI.sub("", text)
    text = re.sub(r"20\d{2}.*", "", text)
    text = text.replace("調教タイム・追い切り情報", "")
    text = text.replace("調教タイム", "")
    text = text.replace("追い切り情報", "")
    return text.replace("ステークス", "S")
