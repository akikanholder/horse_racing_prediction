"""Netkeiba website scraper."""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import date, datetime
from typing import Literal, cast

from bs4 import BeautifulSoup, Tag

from backend.models.course import CourseCondition
from backend.models.horse import (
    PEDIGREE_ABSENT,
    PEDIGREE_FETCH_FAILED,
    PEDIGREE_INCOMPLETE,
    PEDIGREE_SOURCE_BOTH,
    PEDIGREE_SOURCE_PED,
    PEDIGREE_SOURCE_PROFILE,
    PEDIGREE_TABLE_MALFORMED,
    PEDIGREE_TABLE_NOT_FOUND,
    Horse,
    HorsePerformance,
    Pedigree,
    PedigreeHorse,
)
from backend.models.race import (
    Race,
    RaceCard,
    RaceEntry,
    RacePayout,
    RaceRefund,
    RaceResult,
    RaceSchedule,
    ResultPhase,
    ResultRunnerStatus,
    ResultSource,
    SettledFinisher,
    SettledRaceResult,
)
from backend.scrapers.base_scraper import BaseScraper
from backend.scrapers.jra_payout import (
    JRA_PAYOUT_UNREACHABLE,
    JRA_REFUND_MISMATCH,
    OfficialPayoutKey,
    OfficialRefund,
    cross_check_payouts,
)
from backend.scrapers.page_fallback import (
    AccessBlocked,
    FetchedBody,
    PageFallback,
    looks_like_login_wall,
)
from backend.scrapers.runner_status import (
    HEADLESS_UNAVAILABLE,
    NETKEIBA_SHUTUBA_FORBIDDEN,
    NETKEIBA_SHUTUBA_LOGIN,
    SCRATCH_STATUS_MISSING,
    apply_records,
    as_page_source,
    join_warnings,
    records_from_entries,
    row_status,
    same_site_status_urls,
)
from backend.scrapers.runner_status import (
    page_records as _page_records,
)
from backend.utils.date_utils import (
    get_current_week_dates,
    parse_japanese_date,
    parse_post_time,
    tokyo_now,
)
from backend.utils.http_client import HTTPClient

logger = logging.getLogger(__name__)

HORSE_RESULT_FETCH_FAILED = "horse result fetch failed"
HORSE_RESULT_TABLE_NOT_FOUND = "horse result table not found"
HORSE_RESULT_TABLE_MALFORMED = "horse result table malformed"
HORSE_RESULT_EMPTY = "no performances on horse result page"
RACE_RESULT_FETCH_FAILED = "race result fetch failed"
RACE_RESULT_NOT_FOUND = "race result not found"
RACE_RESULT_NOT_SETTLED = "race not yet settled"
RACE_RESULT_TABLE_MALFORMED = "race result table malformed"
RACE_RESULT_EMPTY = "no finishers on race result page"
RACE_RESULT_PAYOUT_MISSING = "払戻に無い馬券種は空のまま"
RACE_RESULT_PAYOUT_UNPAIRED = "払戻の対応が取れない馬券種は空のまま"
RACE_RESULT_PAYOUT_BLANK = "価格が無い払戻行は空のまま"
RACE_RESULT_PAYOUT_ABSENT = "払戻が結果ページに無い"
RACE_RESULT_FORBIDDEN = "race result returned 403"
RACE_RESULT_LOGIN = "race result requires login"
RACE_RESULT_PHASE_UNKNOWN = "result phase was not on the page"
RACE_RESULT_PHASE_DISAGREE = "result phase sources disagree"
RACE_RESULT_HEADLESS_UNAVAILABLE = "headless browser is not installed"
REFUND_NOT_NON_RUNNER = "refund horses are not scratched or excluded"

_STATUS_BY_LABEL = {
    "取消": "scratched",
    "出走取消": "scratched",
    "除外": "excluded",
    "競走除外": "excluded",
    "中止": "did_not_finish",
    "競走中止": "did_not_finish",
    "失格": "disqualified",
}
_RUNNER_LIST_KEYS = ("runners", "results", "horses", "race_result")
_HARD_RESULT_WARNINGS = frozenset(
    {
        RACE_RESULT_FETCH_FAILED,
        RACE_RESULT_NOT_FOUND,
        RACE_RESULT_NOT_SETTLED,
        RACE_RESULT_TABLE_MALFORMED,
        RACE_RESULT_EMPTY,
        RACE_RESULT_FORBIDDEN,
        RACE_RESULT_LOGIN,
    }
)
_SOURCE_ORDER = ("netkeiba_html", "netkeiba_js", "headless")

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
_SETTLED_HEADERS = {
    "着順": "rank",
    "枠": "bracket_number",
    "馬番": "horse_number",
    "馬名": "horse_name",
    "斤量": "weight",
    "騎手": "jockey_name",
    "タイム": "finish_time",
    "着差": "margin",
    "人気": "popularity",
    "単勝オッズ": "odds",
    "後3F": "last_3f",
    "上がり": "last_3f",
    "上り": "last_3f",
    "コーナー通過順": "passing_order",
}
_PAYOUT_BET_TYPES = ("単勝", "複勝", "枠連", "馬連", "ワイド", "馬単", "3連複", "3連単")
_PAYOUT_ALIASES = {"三連複": "3連複", "三連単": "3連単"}
_WEATHER = ("小雨", "晴", "曇", "雨", "雪")
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
    sire_id: str | None = None
    dam_name: str | None = None
    dam_id: str | None = None
    damsire_name: str | None = None
    damsire_id: str | None = None
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


def _horse_id_from_href(href: str) -> str | None:
    """Horse id from a profile link. Pedigree and progeny links are not the horse."""
    path = href.split("?")[0]
    if any(
        part in path for part in ("/horse/ped/", "/horse/sire/", "/horse/result/", "/horse/mare/")
    ):
        return None
    match = re.search(r"/horse/([0-9A-Za-z]+)/?$", path)
    if match is None:
        return None
    return match.group(1)


def _linked_ancestor(cell: Tag) -> tuple[str | None, str | None]:
    """First real horse link in a cell. Year, color, and 血統/産駒 links are skipped."""
    fallback: tuple[str | None, str | None] | None = None
    for anchor in cell.find_all("a"):
        if not isinstance(anchor, Tag):
            continue
        href = str(anchor.get("href") or "")
        name = _optional_text(anchor.get_text())
        if href:
            horse_id = _horse_id_from_href(href)
            if horse_id and name:
                return name, horse_id
            continue
        if name and fallback is None:
            fallback = (name, None)
    return fallback or (None, None)


def _ancestor_from_profile_cell(cell: Tag) -> tuple[str | None, str | None]:
    name, horse_id = _linked_ancestor(cell)
    if name:
        return name, horse_id
    return _optional_text(cell.get_text()), None


def _span(cell: Tag, attr: str) -> int:
    raw = cell.get(attr)
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    try:
        value = int(str(raw or 1))
    except (TypeError, ValueError):
        return 1
    return value if value >= 1 else 1


def _expand_table(table: Tag) -> list[list[Tag | None]]:
    """Expand rowspan and colspan so column 0 is the same generation on every row."""
    rows = [row for row in table.find_all("tr") if isinstance(row, Tag)]
    occupied: dict[tuple[int, int], Tag] = {}
    grid: list[list[Tag | None]] = []
    for row_index, row in enumerate(rows):
        cells = [
            cell for cell in row.find_all(["td", "th"], recursive=False) if isinstance(cell, Tag)
        ]
        line: list[Tag | None] = []
        column = 0
        for cell in cells:
            while (row_index, column) in occupied:
                line.append(occupied.pop((row_index, column)))
                column += 1
            rowspan = _span(cell, "rowspan")
            colspan = _span(cell, "colspan")
            for offset in range(colspan):
                line.append(cell)
                for depth in range(1, rowspan):
                    occupied[(row_index + depth, column + offset)] = cell
            column += colspan
        while (row_index, column) in occupied:
            line.append(occupied.pop((row_index, column)))
            column += 1
        grid.append(line)
    return grid


def _read_blood_table(
    table: Tag,
) -> tuple[
    str, tuple[str | None, str | None], tuple[str | None, str | None], tuple[str | None, str | None]
]:
    """Sire is row 0 col 0. Dam is halfway down column 0. Damsire is the next cell.

    An odd number of rows, or a dam cell that is the same span as the sire,
    is malformed. Names in that table are not used.
    """
    empty: tuple[str | None, str | None] = (None, None)
    grid = _expand_table(table)
    if not grid or not grid[0] or len(grid) % 2 != 0:
        return "malformed", empty, empty, empty
    mid = len(grid) // 2
    sire_cell = grid[0][0]
    if not isinstance(sire_cell, Tag) or mid >= len(grid) or not grid[mid]:
        return "malformed", empty, empty, empty
    dam_cell = grid[mid][0]
    if not isinstance(dam_cell, Tag) or dam_cell is sire_cell:
        return "malformed", empty, empty, empty
    sire = _linked_ancestor(sire_cell)
    dam = _linked_ancestor(dam_cell)
    damsire = empty
    if len(grid[mid]) > 1:
        damsire_cell = grid[mid][1]
        if (
            isinstance(damsire_cell, Tag)
            and damsire_cell is not dam_cell
            and damsire_cell is not sire_cell
        ):
            damsire = _linked_ancestor(damsire_cell)
    return "ok", sire, dam, damsire


def _apply_ancestor(
    current_name: str | None,
    current_id: str | None,
    new_name: str | None,
    new_id: str | None,
) -> tuple[str | None, str | None, bool]:
    """Fill a blank name. Attach an id only when the names match. Never replace a name."""
    if current_name is None and new_name is not None:
        return new_name, new_id, True
    if (
        current_name is not None
        and current_id is None
        and new_id
        and new_name is not None
        and _compact(current_name) == _compact(new_name)
    ):
        return current_name, new_id, True
    return current_name, current_id, False


def _apply_ancestors(
    identity: _HorseIdentity,
    sire: tuple[str | None, str | None],
    dam: tuple[str | None, str | None],
    damsire: tuple[str | None, str | None],
) -> bool:
    identity.sire_name, identity.sire_id, sire_changed = _apply_ancestor(
        identity.sire_name, identity.sire_id, sire[0], sire[1]
    )
    identity.dam_name, identity.dam_id, dam_changed = _apply_ancestor(
        identity.dam_name, identity.dam_id, dam[0], dam[1]
    )
    identity.damsire_name, identity.damsire_id, damsire_changed = _apply_ancestor(
        identity.damsire_name, identity.damsire_id, damsire[0], damsire[1]
    )
    return sire_changed or dam_changed or damsire_changed


def _merge_ped_soup(soup: BeautifulSoup, identity: _HorseIdentity) -> tuple[str, bool]:
    table = soup.find("table", class_=lambda value: bool(value) and "blood_table" in value)
    if not isinstance(table, Tag):
        return "missing", False
    status, sire, dam, damsire = _read_blood_table(table)
    if status != "ok":
        return "malformed", False
    return "found", _apply_ancestors(identity, sire, dam, damsire)


def _pedigree_satisfied(identity: _HorseIdentity) -> bool:
    return all(
        (
            identity.sire_name,
            identity.sire_id,
            identity.dam_name,
            identity.dam_id,
            identity.damsire_name,
            identity.damsire_id,
        )
    )


def _profile_has_pedigree(identity: _HorseIdentity) -> bool:
    return any(
        (
            identity.sire_name,
            identity.sire_id,
            identity.dam_name,
            identity.dam_id,
            identity.damsire_name,
            identity.damsire_id,
        )
    )


def _pedigree_member(name: str | None, horse_id: str | None) -> PedigreeHorse | None:
    if name is None and horse_id is None:
        return None
    return PedigreeHorse(name=name, horse_id=horse_id)


def _assemble_pedigree(
    identity: _HorseIdentity,
    *,
    profile_contributed: bool,
    ped_contributed: bool,
    ped_attempted: bool,
    ped_table: str,
) -> Pedigree:
    sire = _pedigree_member(identity.sire_name, identity.sire_id)
    dam = _pedigree_member(identity.dam_name, identity.dam_id)
    damsire = _pedigree_member(identity.damsire_name, identity.damsire_id)
    if profile_contributed and ped_contributed:
        source: str | None = PEDIGREE_SOURCE_BOTH
    elif ped_contributed:
        source = PEDIGREE_SOURCE_PED
    elif profile_contributed:
        source = PEDIGREE_SOURCE_PROFILE
    else:
        source = None
    if sire is not None and dam is not None:
        warning = None
    elif ped_table == "failed":
        warning = PEDIGREE_FETCH_FAILED
    elif ped_table == "malformed":
        warning = PEDIGREE_TABLE_MALFORMED
    elif ped_attempted and ped_table == "missing":
        warning = PEDIGREE_TABLE_NOT_FOUND
    elif sire is None and dam is None:
        warning = PEDIGREE_ABSENT
    else:
        warning = PEDIGREE_INCOMPLETE
    return Pedigree(sire=sire, dam=dam, damsire=damsire, source=source, warning=warning)


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


def _canonical_payout(label: str) -> str:
    return _PAYOUT_ALIASES.get(label, label)


def _number_token(text: str) -> str | None:
    compact = _compact(text)
    if re.fullmatch(r"\d{1,2}", compact):
        return str(int(compact))
    return None


def _parse_yen(text: str) -> int | None:
    compact = _compact(text).replace(",", "").replace("，", "")
    match = re.fullmatch(r"(\d+)円", compact)
    if match is None:
        return None
    value = int(match.group(1))
    if value <= 0:
        return None
    return value


def _parse_popularity_label(text: str) -> int | None:
    match = re.fullmatch(r"(\d+)人気", _compact(text))
    if match is None:
        return None
    return int(match.group(1))


def _payout_combinations(cell: Tag) -> list[str]:
    """Group horse or bracket numbers the way the 払戻 cell groups them."""
    groups = cell.find_all("ul")
    if groups:
        combinations: list[str] = []
        for group in groups:
            numbers = [
                token
                for token in (_number_token(item.get_text()) for item in group.find_all("li"))
                if token
            ]
            if numbers:
                combinations.append("-".join(numbers))
        return combinations
    return [
        token
        for token in (_number_token(span.get_text()) for span in cell.find_all("span"))
        if token
    ]


def _yen_amounts(cell: Tag) -> list[int | None]:
    chunks: list[str] = []
    spans = cell.find_all("span")
    nodes: list[Tag] = spans or [cell]
    for node in nodes:
        for br in node.find_all("br"):
            br.replace_with("\n")
        chunks.extend(line.strip() for line in node.get_text("\n").splitlines() if line.strip())
    return [_parse_yen(chunk) for chunk in chunks]


def _payout_popularities(cell: Tag) -> list[int | None]:
    spans = cell.find_all("span")
    if not spans:
        return [_parse_popularity_label(cell.get_text())]
    return [_parse_popularity_label(span.get_text()) for span in spans if _compact(span.get_text())]


def _dated_on_page(text: str) -> date | None:
    """Read a full Japanese date. A month-day without a year is not filled in."""
    match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _join_note(prefix: str, labels: list[str]) -> str | None:
    if not labels:
        return None
    return f"{prefix}: " + "、".join(labels)


def _join_warnings(*parts: str | None) -> str | None:
    items = [part for part in parts if part]
    if not items:
        return None
    return "; ".join(items)


_REFUND_BET_NAMES = ("単勝", "複勝", "枠連", "馬連", "ワイド", "馬単", "3連複", "3連単")


def _refund_bits(text: str) -> tuple[list[int], list[int], list[str]]:
    """Horse numbers, brackets, and bet types printed in one 返還 block."""
    compact = _compact(text).replace("三連複", "3連複").replace("三連単", "3連単")
    horses: list[int] = []
    if "返還馬番" in compact:
        head = compact.split("返還馬番", 1)[1].split("返還同枠", 1)[0]
        horses = [int(number) for number in re.findall(r"\d+", head)]
    brackets = [int(number) for number in re.findall(r"返還同枠(\d+)枠", compact)]
    bets = [
        name
        for index, name in sorted(
            (compact.find(name), name) for name in _REFUND_BET_NAMES if name in compact
        )
    ]
    return horses, brackets, bets


def _html_refunds(
    soup: BeautifulSoup, source: str, fetched_at: datetime
) -> list[RaceRefund] | None:
    """返還 rows on a netkeiba result page. A page with none returns null."""
    records: list[RaceRefund] = []
    seen: set[str] = set()
    nodes: list[Tag] = []
    for row in soup.find_all("tr"):
        if not isinstance(row, Tag):
            continue
        label = row.find("th")
        if isinstance(label, Tag) and _compact(label.get_text()) == "返還":
            nodes.append(row)
    for unit in soup.select(".restoration"):
        if isinstance(unit, Tag):
            nodes.append(unit)
    for node in nodes:
        horses, brackets, bets = _refund_bits(node.get_text(" ", strip=True))
        if not horses:
            result = node.find("td", class_="Result")
            if isinstance(result, Tag):
                horses = [
                    int(token)
                    for token in (
                        _number_token(span.get_text()) for span in result.find_all("span")
                    )
                    if token
                ]
                if not horses:
                    horses = [
                        int(number) for number in re.findall(r"\d+", _compact(result.get_text()))
                    ]
        if not horses and not brackets:
            continue
        record = RaceRefund(
            horse_numbers=horses,
            bet_types=bets or None,
            bracket_numbers=brackets or None,
            source=source,
            fetched_at=fetched_at,
        )
        key = f"{horses}|{bets}|{brackets}"
        if key in seen:
            continue
        seen.add(key)
        records.append(record)
    if not records:
        return None
    return records


def _refunds_from_payload(
    payload: dict[str, object], source: str, fetched_at: datetime
) -> list[RaceRefund] | None:
    raw = payload.get("refunds")
    if not isinstance(raw, list) or not raw:
        return None
    records: list[RaceRefund] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        numbers = item.get("horse_numbers", item.get("馬番"))
        if isinstance(numbers, int):
            numbers = [numbers]
        if not isinstance(numbers, list):
            continue
        horses = [int(number) for number in numbers if isinstance(number, int)]
        bets_raw = item.get("bet_types")
        bets = [str(bet) for bet in bets_raw] if isinstance(bets_raw, list) and bets_raw else None
        brackets_raw = item.get("bracket_numbers")
        brackets = (
            [int(number) for number in brackets_raw if isinstance(number, int)]
            if isinstance(brackets_raw, list)
            else None
        )
        if not horses and not brackets:
            continue
        records.append(
            RaceRefund(
                horse_numbers=horses,
                bet_types=bets,
                bracket_numbers=brackets or None,
                source=source,
                fetched_at=fetched_at,
            )
        )
    if not records:
        return None
    return records


def _refund_horses(refunds: list[RaceRefund] | list[OfficialRefund] | None) -> set[int]:
    if not refunds:
        return set()
    numbers: set[int] = set()
    for refund in refunds:
        numbers.update(refund.horse_numbers)
    return numbers


def _refund_bets(refunds: list[RaceRefund] | list[OfficialRefund] | None) -> set[str] | None:
    if not refunds:
        return None
    found: set[str] = set()
    printed = False
    for refund in refunds:
        if refund.bet_types:
            printed = True
            found.update(refund.bet_types)
    if not printed:
        return None
    return found


def _merge_refunds(
    netkeiba: list[RaceRefund] | None,
    official: list[OfficialRefund] | None,
    fetched_at: datetime,
) -> tuple[list[RaceRefund] | None, str | None]:
    """Keep the printed refund. Do not invent horses the page did not name."""
    if not official:
        return netkeiba, None
    jra_records = [
        RaceRefund(
            horse_numbers=list(item.horse_numbers),
            bet_types=list(item.bet_types) if item.bet_types else None,
            bracket_numbers=list(item.bracket_numbers) if item.bracket_numbers else None,
            source="jra_html",
            fetched_at=fetched_at,
        )
        for item in official
    ]
    if not netkeiba:
        return jra_records, None
    if _refund_horses(netkeiba) != _refund_horses(jra_records):
        return netkeiba, _refund_mismatch_text(netkeiba, jra_records)
    net_bets = _refund_bets(netkeiba)
    jra_bets = _refund_bets(jra_records)
    bet_warning = (
        JRA_REFUND_MISMATCH
        if net_bets is not None and jra_bets is not None and net_bets != jra_bets
        else None
    )
    jra_brackets = [
        number for item in jra_records if item.bracket_numbers for number in item.bracket_numbers
    ]
    merged: list[RaceRefund] = []
    for record in netkeiba:
        update: dict[str, object] = {}
        if record.bet_types is None and jra_bets:
            update["bet_types"] = sorted(jra_bets)
        if record.bracket_numbers is None and jra_brackets:
            update["bracket_numbers"] = jra_brackets
            update["source"] = "+".join(part for part in (record.source, "jra_html") if part)
        if update:
            record = record.model_copy(update=update)
        merged.append(record)
    return merged, bet_warning


def _refund_mismatch_text(netkeiba: list[RaceRefund], official: list[RaceRefund]) -> str:
    only_net = sorted(_refund_horses(netkeiba) - _refund_horses(official))
    only_jra = sorted(_refund_horses(official) - _refund_horses(netkeiba))
    parts: list[str] = []
    if only_net:
        parts.append("netkeibaのみ: " + "、".join(f"{number}番" for number in only_net))
    if only_jra:
        parts.append("JRAのみ: " + "、".join(f"{number}番" for number in only_jra))
    if not parts:
        return JRA_REFUND_MISMATCH
    return f"{JRA_REFUND_MISMATCH}: " + "; ".join(parts)


def _refund_status_warning(
    results: list[SettledFinisher], refunds: list[RaceRefund] | None
) -> str | None:
    """A printed refund for a horse that was not 取消 or 除外 is a warning."""
    if not refunds:
        return None
    allowed = {
        row.horse_number
        for row in results
        if row.status in {"scratched", "excluded"} and row.horse_number is not None
    }
    bad: list[str] = []
    seen: set[int] = set()
    for refund in refunds:
        for number in refund.horse_numbers:
            if number in allowed or number in seen:
                continue
            seen.add(number)
            bad.append(str(number))
    if not bad:
        return None
    return f"{REFUND_NOT_NON_RUNNER}: " + "、".join(bad)


def _as_status(value: str | None) -> ResultRunnerStatus | None:
    if value in {"finished", "scratched", "excluded", "did_not_finish", "disqualified", "unknown"}:
        return cast(ResultRunnerStatus, value)
    return None


def _as_source(value: str) -> ResultSource:
    if value in _SOURCE_ORDER:
        return cast(ResultSource, value)
    return "netkeiba_html"


def _as_phase(value: str | None) -> ResultPhase | None:
    if value in {"provisional", "confirmed"}:
        return cast(ResultPhase, value)
    return None


def _result_source(value: str) -> str:
    if value in _SOURCE_ORDER:
        return value
    return "netkeiba_html"


def _result_block_warning(warning: str | None) -> str:
    if warning is not None and "403" in warning:
        return RACE_RESULT_FORBIDDEN
    return RACE_RESULT_LOGIN


def _phase_label(text: str) -> str | None:
    compact = _compact(text)
    if compact in {"確定前", "結果確定前", "未確定", "暫定", "provisional"}:
        return "provisional"
    if compact in {"結果確定", "確定", "confirmed"}:
        return "confirmed"
    return None


_CLASS_LABELS = (
    "新馬",
    "未勝利",
    "1勝クラス",
    "2勝クラス",
    "3勝クラス",
    "１勝クラス",
    "２勝クラス",
    "３勝クラス",
    "オープン",
    "リステッド",
    "L",
    "G1",
    "G2",
    "G3",
    "ＧⅠ",
    "ＧⅡ",
    "ＧⅢ",
    "Jpn1",
    "Jpn2",
    "Jpn3",
    "500万下",
    "1000万下",
    "1600万下",
)


def _race_class_on_page(data02: Tag) -> str | None:
    """Class printed in its own span. The race name and age condition are not a class."""
    for span in data02.find_all("span"):
        if not isinstance(span, Tag):
            continue
        text = _compact(span.get_text())
        if text in _CLASS_LABELS:
            return text
    return None


def _result_phase_on_page(soup: BeautifulSoup) -> str | None:
    """Phase only from a status label on the race header, not from nav or meta text."""
    for node in soup.select(".RaceResultState, [data-result-phase]"):
        if not isinstance(node, Tag):
            continue
        raw = node.get("data-result-phase")
        label = raw if isinstance(raw, str) and raw.strip() else node.get_text()
        phase = _phase_label(str(label))
        if phase is not None:
            return phase
    header = soup.select_one(".RaceList_Item02")
    if not isinstance(header, Tag):
        return None
    if header.select_one(".Race_Provisional") is not None:
        return "provisional"
    if header.select_one(".Race_Fixed") is not None:
        return "confirmed"
    for node in header.find_all(True):
        if not isinstance(node, Tag):
            continue
        own = "".join(str(piece) for piece in node.find_all(string=True, recursive=False))
        phase = _phase_label(own)
        if phase is not None:
            return phase
    return None


def _race_number_on_page(soup: BeautifulSoup) -> int | None:
    """Race number printed in the title. The race id's last digits are not used."""
    if soup.title is None:
        return None
    match = re.search(r"(\d{1,2})\s*[RＲ]", soup.title.get_text())
    if match is None:
        return None
    number = int(match.group(1))
    if 1 <= number <= 12:
        return number
    return None


def _finish_status(rank_text: str) -> tuple[int | None, str | None, str | None, bool]:
    """Rank, status code, printed label, and whether the row says 降着.

    Non-finishers keep a null rank. A demoted horse keeps the official placing.
    """
    demoted = "降着" in rank_text
    compact = _compact(rank_text.replace("降着", ""))
    dead_heat = re.fullmatch(r"(\d+)(?:同着|（同着）|\(同着\)|同)?", compact)
    if dead_heat is not None:
        return int(dead_heat.group(1)), "finished", None, demoted
    code = _STATUS_BY_LABEL.get(compact)
    if code is not None:
        return None, code, compact, demoted
    printed = _compact(rank_text)
    if printed and printed not in _PLACEHOLDERS:
        return None, "unknown", printed, demoted
    return None, None, None, demoted


def _assess_complete(
    results: list[SettledFinisher], entry_count: int | None
) -> tuple[bool, str | None]:
    if entry_count is None:
        return False, "race result incomplete: card runner count was not on the page"
    if len(results) != entry_count:
        return False, f"race result incomplete: {len(results)} of {entry_count} runners"
    missing = [
        str(row.horse_number) if row.horse_number is not None else "?"
        for row in results
        if row.status is None
    ]
    if missing:
        return False, "race result incomplete: runner missing status: " + "、".join(missing)
    return True, None


def _merge_phase(current: str | None, new: str | None) -> tuple[str | None, bool]:
    if new is None:
        return current, False
    if current is None:
        return new, False
    if current != new:
        return None, True
    return current, False


def _runner_key(row: SettledFinisher) -> str:
    if row.horse_number is not None:
        return f"n:{row.horse_number}"
    return f"name:{row.horse_name}"


def _merge_runners(
    base: list[SettledFinisher], extra: list[SettledFinisher]
) -> list[SettledFinisher]:
    """Keep HTML rows, then append runners the table did not have."""
    order: list[str] = []
    rows: dict[str, SettledFinisher] = {}
    for row in base:
        key = _runner_key(row)
        if key not in rows:
            order.append(key)
        rows[key] = row
    for row in extra:
        key = _runner_key(row)
        if key not in rows:
            order.append(key)
            rows[key] = row
            continue
        current = rows[key]
        if current.status is None and row.status is not None:
            rows[key] = current.model_copy(
                update={
                    "rank": row.rank,
                    "status": row.status,
                    "status_label": row.status_label,
                    "demoted": row.demoted or current.demoted,
                    "source": row.source,
                }
            )
    return [rows[key] for key in order]


def _race_source(results: list[SettledFinisher], payouts: list[RacePayout]) -> str | None:
    found = [
        source
        for source in _SOURCE_ORDER
        if any(row.source == source for row in results)
        or any(payout.source == source for payout in payouts)
    ]
    if not found:
        return None
    return "+".join(found)


def _mapping_text(row: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is None or value == "":
            continue
        return str(value)
    return ""


def _finisher_from_mapping(
    row: dict[str, object], source: str, fetched_at: datetime
) -> SettledFinisher | None:
    """One runner object embedded in the page's JSON."""
    rank_text = _mapping_text(row, "rank", "着順", "Rank")
    label_text = _mapping_text(row, "status_label", "区分")
    status_text = _mapping_text(row, "status")
    probe = (
        label_text if label_text and not re.fullmatch(r"\d+", _compact(rank_text)) else rank_text
    )
    if not probe and label_text:
        probe = label_text
    rank, status, status_label, demoted = _finish_status(probe or label_text)
    if "降着" in label_text or "降着" in rank_text or row.get("demoted") is True:
        demoted = True
    explicit = _as_status(status_text)
    if explicit is not None:
        status = explicit
        if explicit in {"scratched", "excluded", "did_not_finish", "disqualified"}:
            rank = None
            if isinstance(label_text, str) and label_text:
                status_label = _compact(label_text)
        elif explicit == "finished":
            status_label = None
    horse_number = _optional_int(_mapping_text(row, "horse_number", "馬番", "Umaban"))
    horse_name = _optional_text(_mapping_text(row, "horse_name", "馬名", "Bamei"))
    if horse_name is None and horse_number is None:
        return None
    horse_id_text = _optional_text(_mapping_text(row, "horse_id", "馬ID"))
    return SettledFinisher(
        rank=rank,
        status=_as_status(status),
        status_label=status_label,
        demoted=demoted,
        bracket_number=_optional_int(_mapping_text(row, "bracket_number", "枠", "Wakuban")),
        horse_number=horse_number,
        horse_name=horse_name,
        horse_id=horse_id_text,
        weight=_optional_float(_mapping_text(row, "weight", "斤量")),
        jockey_name=_optional_text(_mapping_text(row, "jockey_name", "騎手")),
        finish_time=_parse_finish_time(_mapping_text(row, "finish_time", "タイム")),
        margin=_optional_text(_mapping_text(row, "margin", "着差")),
        last_3f=_optional_float(_mapping_text(row, "last_3f", "後3F")),
        popularity=_optional_int(_mapping_text(row, "popularity", "人気")),
        odds=_optional_float(_mapping_text(row, "odds", "単勝オッズ")),
        passing_order=_optional_text(_mapping_text(row, "passing_order", "コーナー通過順")),
        source=_as_source(source),
        fetched_at=fetched_at,
    )


def _venue_code(codes: dict[str, str], name: str) -> str | None:
    for code, venue in codes.items():
        if venue == name:
            return code
    return None


def _official_payout_key(
    header: "_SettledHeader", venue_codes: dict[str, str]
) -> OfficialPayoutKey | None:
    """Match a JRA payout day from values printed on the result page."""
    if (
        header.venue is None
        or header.race_date is None
        or header.kai is None
        or header.meeting_day is None
        or header.race_number is None
    ):
        return None
    code = _venue_code(venue_codes, header.venue)
    if code is None:
        return None
    return OfficialPayoutKey(
        venue_code=code,
        year=header.race_date.year,
        kai=header.kai,
        meeting_day=header.meeting_day,
        race_date=header.race_date,
        race_number=header.race_number,
    )


@dataclass
class _SettledHeader:
    """Race header read from the result page. Missing pieces stay null."""

    race_name: str | None = None
    race_date: date | None = None
    venue: str | None = None
    distance: int | None = None
    track_type: str | None = None
    track_condition: str | None = None
    weather: str | None = None
    race_class: str | None = None
    entry_count: int | None = None
    kai: int | None = None
    meeting_day: int | None = None
    race_number: int | None = None
    result_phase: str | None = None


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

    def __init__(
        self,
        http_client: HTTPClient | None = None,
        *,
        headless_render: Callable[[str], str] | Literal["auto", "off"] = "auto",
    ):
        """Create a scraper.

        ``headless_render="off"`` skips the browser step and does not warn.
        ``"auto"`` uses Playwright when the browser is installed.
        """
        super().__init__(http_client)
        self._headless_render = headless_render

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
            if looks_like_login_wall(str(soup)):
                raise AccessBlocked(401, "requires login")
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
            status = getattr(getattr(e, "response", None), "status_code", None)
            if isinstance(e, AccessBlocked) or status in {401, 403}:
                raise
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
        fetched = tokyo_now()
        fallback = PageFallback(
            self.http_client,
            site="netkeiba",
            headless_render=self._headless_render,
            url_finder=same_site_status_urls,
        )
        loaded = fallback.load(
            url=f"{self.BASE_URL}/race/shutuba.html",
            params={"race_id": race_id},
            headers={**self.BROWSER_HEADERS, "Referer": f"{self.BASE_URL}/top/"},
            encoding=self.ENCODING,
            fetched_at=fetched,
            should_fallback=self._shutuba_needs_fallback,
            still_incomplete=lambda pages: self._shutuba_pages_incomplete(pages, race_id),
        )
        if loaded.status == "blocked" and not loaded.pages:
            logger.warning("Netkeiba shutuba blocked for %s: %s", race_id, loaded.warning)
            return None
        if loaded.status == "failed" or not loaded.pages:
            logger.error("Failed to fetch race card for %s", race_id)
            return None
        try:
            card = self._card_from_shutuba_pages(loaded.pages, race_id)
        except Exception as e:
            logger.error(f"Failed to fetch race card for {race_id}: {e}")
            return None
        if card is None:
            return None
        warning = self._shutuba_warning(card, loaded.warning, loaded.status)
        if warning != card.warning:
            card = card.model_copy(update={"warning": warning})
        return card

    def get_race_result(self, race_id: str) -> list[RaceResult] | None:
        """Get race result rows for CSV export.

        Settlement for ``cli result`` is ``get_settled_race_result``.
        This older reader is not the post-race payout payload.

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

    def get_settled_race_result(
        self, race_id: str, *, fetched_at: datetime | None = None
    ) -> SettledRaceResult:
        """Settled finish and payouts for one race.

        Source is ``https://race.netkeiba.com/race/result.html?race_id=``.
        That page is UTF-8. The horse DB pages stay EUC-JP.
        The Session uses the same Chrome UA as odds and horse fetches.
        This is the settlement of this race, not ``/horse/result/{horse_id}/``.
        A failed fetch, a missing race, or a race that has no finish table
        returns empty finishers and payouts with ``warning``. Prices are not made up.

        When the HTML table is shorter than the printed field, JSON embedded in
        the page or a same-site result URL the page calls is read next, then an
        optional headless render. HTTP 403 and a login wall stop that chain.

        Args:
            race_id: Race ID
            fetched_at: Timestamp stored on the payload. Defaults to now.

        Returns:
            Settled race result. ``warning`` is set when it is not usable.
        """
        fetched = fetched_at or datetime.now()
        fallback = PageFallback(
            self.http_client,
            site="netkeiba",
            headless_render=self._headless_render,
        )
        loaded = fallback.load(
            url=f"{self.BASE_URL}/race/result.html",
            params={"race_id": race_id},
            headers={**self.BROWSER_HEADERS, "Referer": f"{self.BASE_URL}/top/"},
            encoding="utf-8",
            fetched_at=fetched,
            should_fallback=self._result_html_needs_fallback,
            still_incomplete=lambda pages: self._result_pages_incomplete(pages, race_id, fetched),
        )
        if loaded.status == "blocked" and not loaded.pages:
            return self._empty_settled_result(
                race_id,
                fetched,
                _result_block_warning(loaded.warning),
            )
        if loaded.status == "failed" or not loaded.pages:
            return self._empty_settled_result(race_id, fetched, RACE_RESULT_FETCH_FAILED)

        settled = self._settled_from_pages(loaded.pages, race_id, fetched)
        if loaded.status == "blocked":
            settled = settled.model_copy(
                update={
                    "warning": _join_warnings(
                        settled.warning, _result_block_warning(loaded.warning)
                    )
                }
            )
        elif (
            loaded.warning == RACE_RESULT_HEADLESS_UNAVAILABLE
            and not settled.complete
            and settled.warning not in _HARD_RESULT_WARNINGS
        ):
            settled = settled.model_copy(
                update={
                    "warning": _join_warnings(settled.warning, RACE_RESULT_HEADLESS_UNAVAILABLE)
                }
            )
        header = self._settled_header(BeautifulSoup(loaded.pages[0].body, "lxml"))
        return self._with_official_payouts(settled, header)

    def _fetch_race_result_page(self, race_id: str) -> BeautifulSoup:
        """Fetch the race result page with the browser UA and UTF-8."""
        headers = {**self.BROWSER_HEADERS, "Referer": f"{self.BASE_URL}/top/"}
        html = self.http_client.get(
            f"{self.BASE_URL}/race/result.html",
            params={"race_id": race_id},
            headers=headers,
            encoding="utf-8",
        )
        return BeautifulSoup(html, "lxml")

    def _empty_settled_result(
        self,
        race_id: str,
        fetched_at: datetime,
        warning: str,
        header: _SettledHeader | None = None,
    ) -> SettledRaceResult:
        identity = header or _SettledHeader()
        return SettledRaceResult(
            race_id=race_id,
            race_name=identity.race_name,
            race_date=identity.race_date,
            venue=identity.venue,
            distance=identity.distance,
            track_type=identity.track_type,
            track_condition=identity.track_condition,
            weather=identity.weather,
            race_class=identity.race_class,
            entry_count=identity.entry_count,
            fetched_at=fetched_at,
            source=None,
            complete=False,
            result_phase=None,
            results=[],
            payouts=[],
            payout_note=None,
            warning=warning,
        )

    def _parse_settled_race_result(
        self,
        soup: BeautifulSoup,
        race_id: str,
        fetched_at: datetime,
        *,
        source: str = "netkeiba_html",
    ) -> SettledRaceResult:
        """Read one result page. Do not fill blanks from the race id."""
        header = self._settled_header(soup)
        table = soup.find("table", summary="全着順")
        if not isinstance(table, Tag):
            if header.race_name is None and header.race_date is None and header.venue is None:
                return self._empty_settled_result(race_id, fetched_at, RACE_RESULT_NOT_FOUND)
            return self._empty_settled_result(race_id, fetched_at, RACE_RESULT_NOT_SETTLED, header)

        columns = self._settled_columns(table)
        if any(name not in columns for name in ("rank", "horse_number", "horse_name")):
            return self._empty_settled_result(
                race_id, fetched_at, RACE_RESULT_TABLE_MALFORMED, header
            )

        results = self._settled_finishers(table, columns, source=source, fetched_at=fetched_at)
        if not results:
            return self._empty_settled_result(race_id, fetched_at, RACE_RESULT_EMPTY, header)

        payouts, payout_note = self._settled_payouts(soup, source=source, fetched_at=fetched_at)
        return self._result_from_parts(
            race_id,
            fetched_at,
            header,
            results,
            payouts,
            payout_note,
            refunds=_html_refunds(soup, source, fetched_at),
        )

    def _settled_header(self, soup: BeautifulSoup) -> _SettledHeader:
        """Name, date, venue, course, and going as printed on the result page."""
        header = _SettledHeader(race_name=self._settled_race_name(soup))
        header.race_date = self._settled_race_date(soup)
        data02 = soup.select_one(".RaceData02")
        if isinstance(data02, Tag):
            venues = set(self.VENUE_CODES.values())
            data_text = data02.get_text(" ", strip=True)
            for span in data02.find_all("span"):
                venue = _clean(span.get_text())
                if venue in venues:
                    header.venue = venue
                    break
            kai_match = re.search(r"(\d+)回", data_text)
            day_match = re.search(r"(\d+)日目", data_text)
            count_match = re.search(r"(\d+)頭", data_text)
            if kai_match:
                header.kai = int(kai_match.group(1))
            if day_match:
                header.meeting_day = int(day_match.group(1))
            if count_match:
                header.entry_count = int(count_match.group(1))
            header.race_class = _race_class_on_page(data02)
        header.race_number = _race_number_on_page(soup)
        header.result_phase = _result_phase_on_page(soup)
        data01 = soup.select_one(".RaceData01")
        if isinstance(data01, Tag):
            header.distance, header.track_type = self._settled_surface(data01)
            data_text = data01.get_text(" ", strip=True)
            weather_match = re.search(r"天候[:：]\s*(\S+)", data_text)
            if weather_match and weather_match.group(1) in _WEATHER:
                header.weather = weather_match.group(1)
            going_match = re.search(r"馬場[:：]\s*(\S+)", data_text)
            if going_match:
                header.track_condition = _parse_going(going_match.group(1))
        return header

    def _settled_race_name(self, soup: BeautifulSoup) -> str | None:
        node = soup.select_one("h1.RaceName")
        if not isinstance(node, Tag):
            return None
        for icon in node.select(".Icon_GradeType"):
            icon.extract()
        return _optional_text(node.get_text())

    def _settled_race_date(self, soup: BeautifulSoup) -> date | None:
        """Prefer the kaisai_date link. Never decode the date from race_id."""
        active = soup.select_one("#RaceList_DateList dd.Active a")
        if isinstance(active, Tag):
            match = re.search(r"kaisai_date=(\d{4})(\d{2})(\d{2})", str(active.get("href", "")))
            if match:
                try:
                    return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                except ValueError:
                    pass
        texts = []
        if soup.title is not None:
            texts.append(soup.title.get_text())
        description = soup.find("meta", attrs={"name": "description"})
        if isinstance(description, Tag):
            content = description.get("content", "")
            texts.append(content if isinstance(content, str) else "")
        for text in texts:
            found = _dated_on_page(text)
            if found is not None:
                return found
        return None

    def _settled_surface(self, data01: Tag) -> tuple[int | None, str | None]:
        for span in data01.find_all("span"):
            distance, track_type = _parse_distance_cell(span.get_text())
            if distance is not None:
                return distance, track_type
        return _parse_distance_cell(data01.get_text())

    def _settled_columns(self, table: Tag) -> dict[str, int]:
        header_row = table.find("tr")
        columns: dict[str, int] = {}
        if not isinstance(header_row, Tag):
            return columns
        cells = [
            cell
            for cell in header_row.find_all(["th", "td"], recursive=False)
            if isinstance(cell, Tag)
        ]
        for index, cell in enumerate(cells):
            field_name = _SETTLED_HEADERS.get(_header_label(cell.get_text()))
            if field_name is not None and field_name not in columns:
                columns[field_name] = index
        return columns

    def _settled_finishers(
        self,
        table: Tag,
        columns: dict[str, int],
        *,
        source: str,
        fetched_at: datetime,
    ) -> list[SettledFinisher]:
        finishers: list[SettledFinisher] = []
        previous_rank: int | None = None
        for row in table.find_all("tr")[1:]:
            if not isinstance(row, Tag):
                continue
            cols = [cell for cell in row.find_all("td", recursive=False) if isinstance(cell, Tag)]
            if not cols:
                continue
            finisher = self._settled_finisher(
                cols, columns, row=row, source=source, fetched_at=fetched_at
            )
            if finisher is None:
                continue
            rank_text = _compact(_cell_text(cols, columns, "rank"))
            if finisher.rank is None and rank_text in {"同着", "（同着）", "(同着)"}:
                if previous_rank is not None:
                    finisher = finisher.model_copy(
                        update={"rank": previous_rank, "status": "finished", "status_label": None}
                    )
            if finisher.rank is not None:
                previous_rank = finisher.rank
            finishers.append(finisher)
        return finishers

    def _settled_finisher(
        self,
        cols: list[Tag],
        columns: dict[str, int],
        *,
        row: Tag | None = None,
        source: str,
        fetched_at: datetime,
    ) -> SettledFinisher | None:
        horse_cell = _cell(cols, columns, "horse_name")
        horse_name = _optional_text(horse_cell.get_text()) if horse_cell is not None else None
        horse_number = _optional_int(_cell_text(cols, columns, "horse_number"))
        if horse_name is None and horse_number is None:
            return None
        horse_id = None
        if horse_cell is not None:
            for link in horse_cell.find_all("a", href=True):
                match = re.search(r"horse/(\d+)", str(link.get("href", "")))
                if match:
                    horse_id = match.group(1)
                    break
        rank, status, status_label, demoted = _finish_status(_cell_text(cols, columns, "rank"))
        if row is not None and "降着" in row.get_text():
            demoted = True
        return SettledFinisher(
            rank=rank,
            status=_as_status(status),
            status_label=status_label,
            demoted=demoted,
            bracket_number=_optional_int(_cell_text(cols, columns, "bracket_number")),
            horse_number=horse_number,
            horse_name=horse_name,
            horse_id=horse_id,
            weight=_parse_impost(_cell_text(cols, columns, "weight")),
            jockey_name=_optional_text(_cell_text(cols, columns, "jockey_name")),
            finish_time=_parse_finish_time(_cell_text(cols, columns, "finish_time")),
            margin=_optional_text(_cell_text(cols, columns, "margin")),
            last_3f=_optional_float(_cell_text(cols, columns, "last_3f")),
            popularity=_optional_int(_cell_text(cols, columns, "popularity")),
            odds=_optional_float(_cell_text(cols, columns, "odds")),
            passing_order=_optional_text(_cell_text(cols, columns, "passing_order")),
            source=_as_source(source),
            fetched_at=fetched_at,
        )

    def _settled_payouts(
        self,
        soup: BeautifulSoup,
        *,
        source: str,
        fetched_at: datetime,
    ) -> tuple[list[RacePayout], str | None]:
        """Read 払戻 rows. A missing or unpaired price is omitted, not invented."""
        tables = soup.select("table.Payout_Detail_Table")
        if not tables:
            return [], RACE_RESULT_PAYOUT_ABSENT

        payouts: list[RacePayout] = []
        seen: set[str] = set()
        unpaired: list[str] = []
        blank: list[str] = []
        for table in tables:
            for row in table.find_all("tr"):
                if not isinstance(row, Tag):
                    continue
                label_cell = row.find("th")
                if not isinstance(label_cell, Tag):
                    continue
                label = _clean(label_cell.get_text())
                if not label or _compact(label) == "返還":
                    continue
                result_cell = row.find("td", class_="Result")
                payout_cell = row.find("td", class_="Payout")
                if not isinstance(result_cell, Tag) or not isinstance(payout_cell, Tag):
                    unpaired.append(label)
                    continue
                combinations = _payout_combinations(result_cell)
                amounts = _yen_amounts(payout_cell)
                if not combinations or len(combinations) != len(amounts):
                    unpaired.append(label)
                    continue
                popularity_cell = row.find("td", class_="Ninki")
                popularities = (
                    _payout_popularities(popularity_cell)
                    if isinstance(popularity_cell, Tag)
                    else []
                )
                popularity_ok = len(popularities) == len(combinations)
                tickets: list[RacePayout] = []
                dropped_price = False
                for index, (combination, amount) in enumerate(
                    zip(combinations, amounts, strict=True)
                ):
                    if amount is None:
                        dropped_price = True
                        continue
                    popularity = popularities[index] if popularity_ok else None
                    tickets.append(
                        RacePayout(
                            bet_type=label,
                            combination=combination,
                            payout=amount,
                            popularity=popularity,
                            source=_as_source(source),
                            fetched_at=fetched_at,
                        )
                    )
                if not tickets:
                    unpaired.append(label)
                    continue
                payouts.extend(tickets)
                seen.add(_canonical_payout(label))
                if dropped_price:
                    blank.append(label)

        unpaired_keys = {_canonical_payout(label) for label in unpaired}
        blank_keys = {_canonical_payout(label) for label in blank}
        missing = [
            name
            for name in _PAYOUT_BET_TYPES
            if name not in seen and name not in unpaired_keys and name not in blank_keys
        ]
        notes = [
            note
            for note in (
                _join_note(RACE_RESULT_PAYOUT_MISSING, missing),
                _join_note(RACE_RESULT_PAYOUT_UNPAIRED, unpaired),
                _join_note(RACE_RESULT_PAYOUT_BLANK, blank),
            )
            if note
        ]
        return payouts, " ".join(notes) or None

    def _result_html_needs_fallback(self, html: str) -> bool:
        """True when the result table is missing columns, empty, or short of the card."""
        soup = BeautifulSoup(html, "lxml")
        table = soup.find("table", summary="全着順")
        if not isinstance(table, Tag):
            return False
        columns = self._settled_columns(table)
        if any(name not in columns for name in ("rank", "horse_number", "horse_name")):
            return True
        header = self._settled_header(soup)
        rows = self._settled_finishers(
            table,
            columns,
            source="netkeiba_html",
            fetched_at=datetime.min,
        )
        if not rows:
            return True
        if header.entry_count is None:
            return False
        return len(rows) < header.entry_count

    def _result_pages_incomplete(
        self, pages: list[FetchedBody], race_id: str, fetched_at: datetime
    ) -> bool:
        """True when HTML plus JSON still does not cover the card."""
        settled = self._settled_from_pages(pages, race_id, fetched_at)
        if settled.warning in {
            RACE_RESULT_NOT_FOUND,
            RACE_RESULT_NOT_SETTLED,
            RACE_RESULT_FETCH_FAILED,
            RACE_RESULT_FORBIDDEN,
            RACE_RESULT_LOGIN,
        }:
            return False
        return not settled.complete

    def _settled_from_pages(
        self, pages: list[FetchedBody], race_id: str, fetched_at: datetime
    ) -> SettledRaceResult:
        """Merge the HTML table with later JSON or headless documents."""
        first = pages[0]
        soup = BeautifulSoup(first.body, "lxml")
        base = self._parse_settled_race_result(
            soup, race_id, first.fetched_at, source=_result_source(first.source)
        )
        header = self._settled_header(soup)
        runners = list(base.results)
        phase = header.result_phase
        phase_disagree = False
        entry_count = header.entry_count
        payouts = list(base.payouts)
        payout_note = base.payout_note
        refunds = base.refunds
        for page in pages[1:]:
            extra_runners, extra_phase, extra_count, extra_payouts, extra_note, extra_refunds = (
                self._supplement_from_page(page, race_id)
            )
            if extra_runners:
                runners = _merge_runners(runners, extra_runners)
            if extra_count is not None and entry_count is None:
                entry_count = extra_count
            phase, disagreed = _merge_phase(phase, extra_phase)
            phase_disagree = phase_disagree or disagreed
            if extra_payouts and not payouts:
                payouts = extra_payouts
                payout_note = extra_note
            if extra_refunds and refunds is None:
                refunds = extra_refunds
        if not runners:
            return base
        header.entry_count = entry_count
        header.result_phase = None if phase_disagree else phase
        return self._result_from_parts(
            race_id,
            fetched_at,
            header,
            runners,
            payouts,
            payout_note,
            refunds=refunds,
            phase_disagree=phase_disagree,
        )

    def _supplement_from_page(
        self, page: FetchedBody, race_id: str
    ) -> tuple[
        list[SettledFinisher],
        str | None,
        int | None,
        list[RacePayout],
        str | None,
        list[RaceRefund] | None,
    ]:
        text = page.body.lstrip()
        source = _result_source(page.source)
        if not text.startswith("<"):
            runners, phase, entry_count, refunds = self._runners_from_json(
                page.body, source, page.fetched_at
            )
            return runners, phase, entry_count, [], None, refunds
        soup = BeautifulSoup(page.body, "lxml")
        parsed = self._parse_settled_race_result(soup, race_id, page.fetched_at, source=source)
        header = self._settled_header(soup)
        return (
            list(parsed.results),
            header.result_phase,
            header.entry_count,
            list(parsed.payouts),
            parsed.payout_note,
            parsed.refunds,
        )

    def _runners_from_json(
        self, body: str, source: str, fetched_at: datetime
    ) -> tuple[list[SettledFinisher], str | None, int | None, list[RaceRefund] | None]:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return [], None, None, None
        if not isinstance(payload, dict):
            return [], None, None, None
        raw_rows: list[object] | None = None
        for key in _RUNNER_LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                raw_rows = value
                break
        runners: list[SettledFinisher] = []
        if raw_rows is not None:
            for item in raw_rows:
                if not isinstance(item, dict):
                    continue
                runner = _finisher_from_mapping(item, source, fetched_at)
                if runner is not None:
                    runners.append(runner)
        phase = _phase_label(str(payload.get("result_phase") or ""))
        count = payload.get("entry_count")
        entry_count = count if isinstance(count, int) else None
        return runners, phase, entry_count, _refunds_from_payload(payload, source, fetched_at)

    def _result_from_parts(
        self,
        race_id: str,
        fetched_at: datetime,
        header: _SettledHeader,
        results: list[SettledFinisher],
        payouts: list[RacePayout],
        payout_note: str | None,
        *,
        refunds: list[RaceRefund] | None = None,
        phase_disagree: bool = False,
    ) -> SettledRaceResult:
        complete, incomplete = _assess_complete(results, header.entry_count)
        phase_warning: str | None = None
        phase = None if phase_disagree else header.result_phase
        if phase_disagree:
            phase_warning = RACE_RESULT_PHASE_DISAGREE
        elif phase is None:
            phase_warning = RACE_RESULT_PHASE_UNKNOWN
        return SettledRaceResult(
            race_id=race_id,
            race_name=header.race_name,
            race_date=header.race_date,
            venue=header.venue,
            distance=header.distance,
            track_type=header.track_type,
            track_condition=header.track_condition,
            weather=header.weather,
            race_class=header.race_class,
            entry_count=header.entry_count,
            fetched_at=fetched_at,
            source=_race_source(results, payouts),
            complete=complete,
            result_phase=_as_phase(phase),
            results=results,
            payouts=payouts,
            refunds=refunds,
            payout_note=payout_note,
            warning=_join_warnings(incomplete, phase_warning),
        )

    def _with_official_payouts(
        self, settled: SettledRaceResult, header: _SettledHeader
    ) -> SettledRaceResult:
        """Warn when JRA's public payout list is missing or disagrees. Do not invent prices."""
        if settled.warning in _HARD_RESULT_WARNINGS:
            return settled
        refunds = settled.refunds
        notes: list[str | None] = []
        if settled.payouts or settled.refunds:
            key = _official_payout_key(header, self.VENUE_CODES)
            if key is None:
                notes.append(JRA_PAYOUT_UNREACHABLE)
                official: list[OfficialRefund] | None = None
            else:
                payout_note, official = cross_check_payouts(
                    self.http_client,
                    key,
                    [
                        (payout.bet_type, payout.combination, payout.payout, payout.popularity)
                        for payout in settled.payouts
                    ],
                )
                notes.append(payout_note)
            refunds, refund_note = _merge_refunds(settled.refunds, official, settled.fetched_at)
            notes.append(refund_note)
        notes.append(_refund_status_warning(settled.results, refunds))
        warning = _join_warnings(settled.warning, *notes)
        if warning == settled.warning and refunds == settled.refunds:
            return settled
        return settled.model_copy(update={"warning": warning, "refunds": refunds})

    def get_horse_info(self, horse_id: str, *, fetched_at: datetime | None = None) -> Horse | None:
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

        pedigree = self._resolve_pedigree(
            horse_id,
            identity,
            profile_contributed=_profile_has_pedigree(identity),
        )
        return Horse(
            horse_id=horse_id,
            horse_name=identity.horse_name or "不明",
            english_name=identity.english_name,
            sex=identity.sex or "不明",
            birth_date=identity.birth_date,
            birth_year=identity.birth_year,
            coat_color=identity.coat_color,
            sire_name=pedigree.sire.name if pedigree.sire else None,
            sire_id=pedigree.sire.horse_id if pedigree.sire else None,
            dam_name=pedigree.dam.name if pedigree.dam else None,
            dam_id=pedigree.dam.horse_id if pedigree.dam else None,
            damsire_name=pedigree.damsire.name if pedigree.damsire else None,
            damsire_id=pedigree.damsire.horse_id if pedigree.damsire else None,
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
            pedigree=pedigree,
        )

    def get_pedigree(self, horse_id: str) -> Pedigree:
        """Pedigree from ``/horse/ped/{horse_id}/`` only.

        The race-horses payload does not open the profile page. A miss stays
        null and sets ``warning``. Names are not taken from other tables.
        """
        return self._resolve_pedigree(horse_id, _HorseIdentity(), profile_contributed=False)

    def _resolve_pedigree(
        self,
        horse_id: str,
        identity: _HorseIdentity,
        *,
        profile_contributed: bool,
    ) -> Pedigree:
        """Fill blanks from the ped page. Names already found are kept."""
        ped_attempted = False
        ped_table = "skipped"
        ped_contributed = False
        if not _pedigree_satisfied(identity):
            ped_attempted = True
            url = f"{self.DB_URL}/horse/ped/{horse_id}/"
            try:
                soup = self._fetch_horse_page(url, referer=f"{self.DB_URL}/horse/{horse_id}/")
            except Exception as e:
                logger.error("Failed to fetch pedigree for %s: %s", horse_id, e)
                ped_table = "failed"
            else:
                ped_table, ped_contributed = _merge_ped_soup(soup, identity)
        return _assemble_pedigree(
            identity,
            profile_contributed=profile_contributed,
            ped_contributed=ped_contributed,
            ped_attempted=ped_attempted,
            ped_table=ped_table,
        )

    def get_horse_performances(self, horse_id: str, limit: int = 10) -> list[HorsePerformance]:
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
                self.clean_text(race_name_elem.get_text()) if race_name_elem else f"Race {race_id}"
            )

            race_number = self._extract_race_number(race_id)
            race_date = self._extract_date_from_race_id(race_id) or date.today()
            venue_code, venue, race_data_text = self._confirmed_meeting(soup, race_id)
            if venue == "不明":
                distance, track_type = None, None
                track_condition = None
                weather = None
                post_time = None
            else:
                distance, track_type = self._extract_distance_info(race_data_text)
                track_condition = None
                going_match = re.search(r"馬場[:：]\s*(\S+)", race_data_text)
                if going_match:
                    track_condition = _parse_going(going_match.group(1))
                weather = None
                for w in ["晴", "曇", "雨", "小雨", "雪"]:
                    if w in race_data_text:
                        weather = w
                        break
                post_time = parse_post_time(race_data_text)

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
                post_time_source="netkeiba_html" if post_time else None,
                post_time_fetched_at=tokyo_now() if post_time else None,
            )

        except Exception as e:
            logger.warning(f"Failed to parse race info: {e}")
            return None

    def _confirmed_meeting(self, soup: BeautifulSoup, race_id: str) -> tuple[str | None, str, str]:
        """Return venue code, name, and RaceData01 text when the page is that meeting.

        The name is copied only from a ``RaceData02`` span. Nav chrome and a
        403 body are ignored. A netkeiba id's day field is not a venue code:
        day 2 is ``02``, which is 函館 only when the header actually says so.
        """
        page_venue = self._meeting_header_venue(soup)
        requested_code, requested_name = self._venue_requested_by_race_id(race_id)
        if page_venue is None:
            return None, "不明", ""
        if requested_name is not None and page_venue != requested_name:
            return None, "不明", ""
        code = requested_code
        if code is None:
            code = next(
                (venue_code for venue_code, name in self.VENUE_CODES.items() if name == page_venue),
                None,
            )
        data01 = soup.select_one(".RaceData01")
        text = self.clean_text(data01.get_text()) if isinstance(data01, Tag) else ""
        return code, page_venue, text

    def _meeting_header_venue(self, soup: BeautifulSoup) -> str | None:
        """Venue printed in the meeting header. One name, or none."""
        data02 = soup.select_one(".RaceData02")
        if not isinstance(data02, Tag):
            return None
        venues = set(self.VENUE_CODES.values())
        found: list[str] = []
        for span in data02.find_all("span"):
            name = self.clean_text(span.get_text())
            if name in venues and name not in found:
                found.append(name)
        if len(found) == 1:
            return found[0]
        return None

    def _venue_requested_by_race_id(self, race_id: str) -> tuple[str | None, str | None]:
        """Venue of a netkeiba id (year+venue+kai+day+race) or a project id.

        Netkeiba is used when kai is 1-6 and the day is 1-12. The day digits
        are not read as a venue code.
        """
        if len(race_id) < 12 or not race_id[:12].isdigit():
            return None, None
        body = race_id[:12]
        netkeiba_code = body[4:6]
        kai = int(body[6:8])
        day_number = int(body[8:10])
        if netkeiba_code in self.VENUE_CODES and 1 <= kai <= 6 and 1 <= day_number <= 12:
            return netkeiba_code, self.VENUE_CODES[netkeiba_code]
        try:
            date(int(body[:4]), int(body[4:6]), int(body[6:8]))
        except ValueError:
            return None, None
        project_code = body[8:10]
        if project_code in self.VENUE_CODES:
            return project_code, self.VENUE_CODES[project_code]
        return None, None

    def _find_shutuba_table(self, soup: BeautifulSoup) -> Tag | None:
        """Starter table. A results table is not the card."""
        table = soup.find("table", class_=re.compile(r"Shutuba|shutuba|race_table"))
        if not isinstance(table, Tag):
            found = soup.find("table", id=re.compile(r"shutuba"))
            table = found if isinstance(found, Tag) else None
        return table

    def _shutuba_needs_fallback(self, html: str) -> bool:
        """True when the starter table is there but status was not on it."""
        soup = BeautifulSoup(html, "lxml")
        if self._find_shutuba_table(soup) is None:
            return False
        entries = self._parse_race_entries(soup, source="netkeiba_html", fetched_at=datetime.min)
        if not entries:
            return True
        return any(entry.status is None for entry in entries)

    def _shutuba_pages_incomplete(self, pages: list[FetchedBody], race_id: str) -> bool:
        card = self._card_from_shutuba_pages(pages, race_id)
        if card is None or not card.entries:
            return bool(
                card is None or self._find_shutuba_table(BeautifulSoup(pages[0].body, "lxml"))
            )
        return any(entry.status is None for entry in card.entries)

    def _card_from_shutuba_pages(self, pages: list[FetchedBody], race_id: str) -> RaceCard | None:
        """Merge the shutuba table with later JSON. Missing status stays null."""
        card: RaceCard | None = None
        for page in pages:
            source = as_page_source(page.source)
            if not page.body.lstrip().startswith(("{", "[")):
                soup = BeautifulSoup(page.body, "lxml")
                race = self._parse_race_info(soup, race_id)
                entries = self._parse_race_entries(soup, source=source, fetched_at=page.fetched_at)
                if race is not None and card is None:
                    card = RaceCard(race=race, entries=entries)
                elif card is not None and entries:
                    card = card.model_copy(
                        update={
                            "entries": apply_records(
                                card.entries,
                                records_from_entries(entries),
                                source=page.source,
                                fetched_at=page.fetched_at,
                            )
                        }
                    )
            records = _page_records(page.body)
            if card is not None and records:
                card = card.model_copy(
                    update={
                        "entries": apply_records(
                            card.entries,
                            records,
                            source=page.source,
                            fetched_at=page.fetched_at,
                        )
                    }
                )
        return card

    def _shutuba_warning(
        self, card: RaceCard, chain_warning: str | None, status: str
    ) -> str | None:
        """A printed status for every starter drops the fallback warning."""
        if card.entries and all(entry.status is not None for entry in card.entries):
            return None
        blocked = None
        if status == "blocked":
            blocked = (
                NETKEIBA_SHUTUBA_FORBIDDEN
                if chain_warning and "403" in chain_warning
                else NETKEIBA_SHUTUBA_LOGIN
            )
        headless = HEADLESS_UNAVAILABLE if chain_warning == HEADLESS_UNAVAILABLE else None
        if not card.entries and blocked is None and headless is None:
            return None
        missing = SCRATCH_STATUS_MISSING if card.entries else None
        return join_warnings(blocked, headless, missing)

    def _parse_race_entries(
        self,
        soup: BeautifulSoup,
        *,
        source: str = "netkeiba_html",
        fetched_at: datetime | None = None,
    ) -> list[RaceEntry]:
        """Parse race entries from shutuba page."""
        entries = []
        fetched = fetched_at or tokyo_now()
        page_source = as_page_source(source)

        table = self._find_shutuba_table(soup)
        if not table:
            return entries

        rows = table.find_all("tr")
        for row in rows:
            if not isinstance(row, Tag):
                continue
            cols = row.find_all(["td", "th"])
            if len(cols) < 4:
                continue

            try:
                entry = self._parse_entry_row(row, cols, source=page_source, fetched_at=fetched)
                if entry:
                    entries.append(entry)
            except Exception as e:
                logger.warning(f"Failed to parse entry row: {e}")

        return entries

    def _parse_entry_row(
        self,
        row: Tag,
        cols: list[Tag],
        *,
        source: str,
        fetched_at: datetime,
    ) -> RaceEntry | None:
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

            printed = row_status(row)
            status = printed[0] if printed is not None else "entered"
            status_label = printed[1] if printed is not None else None
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
                status=status,
                status_label=status_label,
                source=as_page_source(source),
                fetched_at=fetched_at,
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
        profile, ancestors = self._profile_table(soup)
        table_sex = _normalize_sex(profile.get("性別", ""))
        if table_sex:
            identity.sex = table_sex
        birth = parse_japanese_date(profile.get("生年月日", ""))
        if birth:
            identity.birth_date = birth
            identity.birth_year = birth.year
        identity.trainer_name, identity.training_center = _parse_trainer(profile.get("調教師", ""))
        identity.owner_name = _optional_text(profile.get("馬主", ""))
        identity.breeder_name = _optional_text(profile.get("生産者", ""))
        identity.breeder_location = _optional_text(profile.get("産地", ""))
        identity.sire_name, identity.sire_id = ancestors.get("父", (None, None))
        identity.dam_name, identity.dam_id = ancestors.get("母", (None, None))
        identity.damsire_name, identity.damsire_id = ancestors.get("母父", (None, None))
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

    def _profile_table(
        self, soup: BeautifulSoup
    ) -> tuple[dict[str, str], dict[str, tuple[str | None, str | None]]]:
        table = soup.find("table", class_=re.compile(r"db_prof_table|profile"))
        data: dict[str, str] = {}
        ancestors: dict[str, tuple[str | None, str | None]] = {}
        if not isinstance(table, Tag):
            return data, ancestors
        for row in table.find_all("tr"):
            header = row.find("th")
            value = row.find("td")
            if isinstance(header, Tag) and isinstance(value, Tag):
                label = _profile_label(header.get_text())
                data[label] = value.get_text()
                if label in {"父", "母", "母父"}:
                    ancestors[label] = _ancestor_from_profile_cell(value)
        return data, ancestors

    def _fill_blood(self, soup: BeautifulSoup, identity: _HorseIdentity) -> None:
        table = soup.find("table", class_=lambda value: bool(value) and "blood_table" in value)
        if isinstance(table, Tag):
            status, sire, dam, damsire = _read_blood_table(table)
            if status == "ok":
                _apply_ancestors(identity, sire, dam, damsire)
            return
        blood = soup.find(
            "table",
            class_=lambda value: bool(value) and "blood" in value and "blood_table" not in value,
        )
        if not isinstance(blood, Tag):
            return
        ancestors: list[tuple[str | None, str | None]] = []
        for anchor in blood.find_all("a"):
            if not isinstance(anchor, Tag):
                continue
            href = str(anchor.get("href") or "")
            horse_id = _horse_id_from_href(href) if href else None
            if href and horse_id is None:
                continue
            name = _optional_text(anchor.get_text())
            if name:
                ancestors.append((name, horse_id))
        # Two links in one cell are sire then dam. A third link is not damsire.
        if len(ancestors) >= 1:
            _apply_ancestors(identity, ancestors[0], (None, None), (None, None))
        if len(ancestors) >= 2:
            _apply_ancestors(identity, (None, None), ancestors[1], (None, None))

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

    def _parse_track_condition(self, element: Tag, venue: str) -> CourseCondition | None:
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
            if netkeiba_venue in self.VENUE_CODES and 1 <= kai <= 6 and 1 <= day_number <= 12:
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
