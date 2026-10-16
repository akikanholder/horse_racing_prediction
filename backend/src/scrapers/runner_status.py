"""Scratch and exclusion marks for the race card and odds.

JRA is preferred, then netkeiba. Callers use the public-page fallback
(HTML, JSON already in the page, a same-site URL the page calls, then
optional headless). A 403 or a login wall stops that chain. This module
does not change the User-Agent, solve a captcha, or send credentials.
A price the page did not print stays null.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from backend.models.odds import NonRunner, OddsQuote, RaceOdds
from backend.models.race import CardRunnerStatus, PageFetchSource, RaceEntry

logger = logging.getLogger(__name__)

SCRATCH_STATUS_MISSING = "scratch status was not on the page"
JRA_SHUTUBA_FORBIDDEN = "JRA shutuba returned 403"
JRA_SHUTUBA_LOGIN = "JRA shutuba requires login"
NETKEIBA_SHUTUBA_FORBIDDEN = "netkeiba shutuba returned 403"
NETKEIBA_SHUTUBA_LOGIN = "netkeiba shutuba requires login"
JRA_ODDS_FORBIDDEN = "JRA odds returned 403"
JRA_ODDS_LOGIN = "JRA odds requires login"
NETKEIBA_ODDS_FORBIDDEN = "netkeiba odds returned 403"
NETKEIBA_ODDS_LOGIN = "netkeiba odds requires login"
JRA_ODDS_FETCH_FAILED = "JRA odds fetch failed"
HEADLESS_UNAVAILABLE = "headless browser is not installed"

_CARD_LABELS: tuple[tuple[str, CardRunnerStatus], ...] = (
    ("出走取消", "scratched"),
    ("競走除外", "excluded"),
    ("取消", "scratched"),
    ("除外", "excluded"),
)
_NON_RUNNER = frozenset({"scratched", "excluded", "unknown"})
_PLACEHOLDERS = frozenset({"", "-", "--", "---", "**", "*", "----", "---.-", "－", "―"})
_WITHHELD = frozenset({"----", "－－－－"})
_STATUS_CLASS = ("cancel", "scratch", "exclude", "jogai", "torikeshi", "nonrunner", "non-runner")
# A past-race finish and the non-selected-horse block are not this race's mark.
_IGNORED_STATUS_CONTEXT = frozenset({"past", "exclusion_block"})
_CLASS_SPLIT = re.compile(r"[-_\s]+")
_CEILING_ODDS = 9999.9
_CEILING_TOLERANCE = 0.05
_RUNNER_KEYS = ("runners", "horses", "entries", "non_runners", "scratched", "cancels")
_JSON_URL = re.compile(r"""(?:url|URL|href)\s*[:=]\s*['"]([^'"]+)['"]""")
_STATUS_PATH = re.compile(r"(shutuba|scratch|cancel|nonrunner|odds|access[do])", re.IGNORECASE)
_LABEL_TEXT = re.compile(r"(?:\d{1,2}番)?(出走取消|競走除外|取消|除外)\Z")
_HORSE_NUMBER = re.compile(r"^(?:[1-9]|1[0-8])$")
_COMBINATION = re.compile(
    r"^(?:[1-9]|1[0-8])(?:-(?:[1-9]|1[0-8])){0,2}$"
)


def compact(text: str) -> str:
    """Drop whitespace. The printed label is compared without gaps."""
    return re.sub(r"\s+", "", text or "")


def join_warnings(*parts: str | None) -> str | None:
    """Join reasons with ``; ``. Empty pieces are left out."""
    found: list[str] = []
    for part in parts:
        if not part:
            continue
        for piece in part.split("; "):
            text = piece.strip()
            if text and text not in found:
                found.append(text)
    if not found:
        return None
    return "; ".join(found)


def status_from_token(text: str) -> tuple[CardRunnerStatus, str] | None:
    """Map a short printed token onto a status code and the label as printed.

    ``出走取消`` stays that label, not the shorter ``取消`` inside it.
    A sentence that merely mentions 取消 is not a mark.
    """
    token = compact(text)
    if not token or len(token) > 16:
        return None
    match = _LABEL_TEXT.fullmatch(token)
    if match is None:
        return None
    label = match.group(1)
    for printed, code in _CARD_LABELS:
        if printed == label:
            return code, label
    return None


def is_withheld_token(text: str) -> bool:
    """True when the cell is the printed blank ``----``, not a number."""
    return compact(text) in _WITHHELD


def as_page_source(value: str) -> PageFetchSource:
    """Keep a fallback source token. Anything else is treated as HTML."""
    if value in {"jra_html", "jra_js", "netkeiba_html", "netkeiba_js", "headless"}:
        return value  # type: ignore[return-value]
    if value.endswith("_js"):
        return "netkeiba_js" if value.startswith("netkeiba") else "jra_js"
    if value == "headless":
        return "headless"
    if value.startswith("jra"):
        return "jra_html"
    return "netkeiba_html"


def row_status(row: Tag) -> tuple[CardRunnerStatus, str] | None:
    """Status printed on a starter or odds row.

    A known label wins. A status element with some other short text is
    ``unknown`` and keeps that text. A row with no mark returns None
    (the caller may then record ``entered``).
    """
    for cell in row.find_all(["td", "th", "span", "div", "p"]):
        if not isinstance(cell, Tag) or _ignored_status_context(cell):
            continue
        own = compact("".join(str(piece) for piece in cell.find_all(string=True, recursive=False)))
        hit = status_from_token(own) or status_from_token(compact(cell.get_text(" ", strip=True)))
        if hit is not None and _short_status_cell(cell):
            return hit
    for cell in row.find_all(True):
        if not isinstance(cell, Tag) or _ignored_status_context(cell):
            continue
        if not _status_element(cell):
            continue
        printed = compact(cell.get_text(" ", strip=True))
        hit = status_from_token(printed)
        if hit is not None:
            return hit
        if printed and printed not in _PLACEHOLDERS and not _looks_like_number(printed):
            if len(printed) <= 12:
                return "unknown", printed
    raw = row.get("data-runner-status") or row.get("data-status")
    if isinstance(raw, str):
        hit = status_from_token(raw)
        if hit is not None:
            return hit
        token = compact(raw)
        if token and token not in _PLACEHOLDERS and len(token) <= 12:
            return "unknown", token
    return None


def _short_status_cell(cell: Tag) -> bool:
    """A cell that is only the label, or a label with a horse number."""
    text = compact(cell.get_text(" ", strip=True))
    if len(text) > 16:
        return False
    return status_from_token(text) is not None


def _class_tokens(node: Tag) -> set[str]:
    """Class names, plus the pieces of ``Cancel_Txt`` and ``non-runner``.

    ``exclusion_block`` stays ``exclusion`` and ``block``. It does not match
    the status token ``exclude``.
    """
    raw = node.get("class") or []
    if isinstance(raw, str):
        raw = raw.split()
    tokens: set[str] = set()
    for item in raw:
        text = str(item).lower()
        if not text:
            continue
        tokens.add(text)
        for part in _CLASS_SPLIT.split(text):
            if part:
                tokens.add(part)
    return tokens


def _ignored_status_context(node: Tag) -> bool:
    """True when this node sits in a past run or the non-selected block."""
    current: Tag | None = node
    while isinstance(current, Tag):
        if _class_tokens(current) & _IGNORED_STATUS_CONTEXT:
            return True
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return False


def _status_element(cell: Tag) -> bool:
    if any(token in _class_tokens(cell) for token in _STATUS_CLASS):
        return True
    return cell.get("data-runner-status") is not None or cell.get("data-status") is not None


def _looks_like_number(text: str) -> bool:
    try:
        float(text.replace(",", ""))
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class StatusRecord:
    """One horse's printed pre-race status."""

    horse_number: int
    status: CardRunnerStatus
    status_label: str | None
    horse_name: str | None = None
    bracket_number: int | None = None


def records_from_json(body: str) -> list[StatusRecord]:
    """Runner status embedded in a page or returned by a same-site URL.

    Unrelated JSON is ignored. Horse numbers that are not on the page are
    not created from other fields.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return []
    rows = _runner_rows(payload)
    records: list[StatusRecord] = []
    seen: set[int] = set()
    for row in rows:
        record = _record_from_mapping(row)
        if record is None or record.horse_number in seen:
            continue
        seen.add(record.horse_number)
        records.append(record)
    return records


def _runner_rows(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in _RUNNER_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            return [row for row in value.values() if isinstance(row, dict)]
    data = payload.get("data")
    if isinstance(data, dict):
        return _runner_rows(data)
    return []


def _record_from_mapping(row: dict[str, object]) -> StatusRecord | None:
    number = _optional_int(row.get("horse_number") or row.get("馬番") or row.get("num"))
    if number is None or not 1 <= number <= 18:
        return None
    label_text = _text(row.get("status_label") or row.get("区分") or row.get("label"))
    status_text = _text(row.get("status"))
    hit = status_from_token(label_text) or status_from_token(status_text)
    status: CardRunnerStatus | None
    label: str | None
    if hit is not None:
        status, label = hit
    elif status_text in {"entered", "scratched", "excluded", "unknown"}:
        status = status_text  # type: ignore[assignment]
        label = label_text or None
        if status == "entered":
            label = None
        elif status in _NON_RUNNER and label_text:
            parsed = status_from_token(label_text)
            label = parsed[1] if parsed is not None else label_text
    else:
        return None
    if status is None:
        return None
    name = _text(row.get("horse_name") or row.get("馬名") or row.get("name")) or None
    bracket = _optional_int(row.get("bracket_number") or row.get("枠") or row.get("waku"))
    if bracket is not None and not 1 <= bracket <= 8:
        bracket = None
    return StatusRecord(
        horse_number=number,
        status=status,
        status_label=label,
        horse_name=name,
        bracket_number=bracket,
    )


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text.isdigit():
        return None
    return int(text)


def records_from_entries(entries: list[RaceEntry]) -> list[StatusRecord]:
    """Status already parsed from a starter row."""
    records: list[StatusRecord] = []
    for entry in entries:
        if entry.status is None:
            continue
        records.append(
            StatusRecord(
                horse_number=entry.horse_number,
                status=entry.status,
                status_label=entry.status_label,
                horse_name=entry.horse_name,
                bracket_number=entry.bracket_number,
            )
        )
    return records


def apply_records(
    entries: list[RaceEntry],
    records: list[StatusRecord],
    *,
    source: str,
    fetched_at: datetime,
) -> list[RaceEntry]:
    """Fill missing status. Do not replace a status the earlier page printed."""
    page_source = as_page_source(source)
    by_number = {entry.horse_number: entry for entry in entries}
    order = [entry.horse_number for entry in entries]
    for record in records:
        current = by_number.get(record.horse_number)
        if current is None:
            if not record.horse_name or record.bracket_number is None:
                continue
            order.append(record.horse_number)
            by_number[record.horse_number] = RaceEntry(
                bracket_number=record.bracket_number,
                horse_number=record.horse_number,
                horse_name=record.horse_name,
                status=record.status,
                status_label=record.status_label,
                source=page_source,
                fetched_at=fetched_at,
            )
            continue
        if current.status is not None:
            continue
        by_number[record.horse_number] = current.model_copy(
            update={
                "status": record.status,
                "status_label": record.status_label,
                "source": page_source,
                "fetched_at": fetched_at,
            }
        )
    return [by_number[number] for number in order]


def same_site_status_urls(html: str, page_url: str) -> list[str]:
    """Same-site URLs the page's own script calls for starters or odds.

    Login endpoints and other hosts are not included.
    """
    found: list[str] = []
    seen: set[str] = set()
    page = urlparse(page_url)
    for match in _JSON_URL.finditer(html):
        raw = match.group(1).strip()
        path = raw.split("?", 1)[0]
        if not _STATUS_PATH.search(path):
            continue
        if re.search(r"login|captcha|signin", raw, re.IGNORECASE):
            continue
        absolute = urljoin(page_url, raw)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or parsed.netloc != page.netloc:
            continue
        if absolute in seen or absolute.split("?", 1)[0] == page_url.split("?", 1)[0]:
            continue
        seen.add(absolute)
        found.append(absolute)
    return found


@dataclass
class OddsMarks:
    """Scratch marks and withheld prices printed on one odds document."""

    runners: list[NonRunner] = field(default_factory=list)
    withheld: set[str] = field(default_factory=set)
    saw_price: bool = False
    saw_table: bool = False

    @property
    def complete(self) -> bool:
        """True when the document printed a price or an explicit mark."""
        return self.saw_price or bool(self.runners) or bool(self.withheld)


def marks_from_html(html: str, *, source: str, fetched_at: datetime) -> OddsMarks:
    """Read scratch labels and ``----`` cells. Do not invent a price."""
    soup = BeautifulSoup(html, "lxml")
    marks = OddsMarks()
    page_source = as_page_source(source)
    for table in soup.find_all("table"):
        if not isinstance(table, Tag):
            continue
        marks.saw_table = True
        _marks_from_table(table, marks, source=page_source, fetched_at=fetched_at)
    return marks


def records_from_json_blocks(html: str) -> list[StatusRecord]:
    """Status objects in ``<script type=application/json>`` and bare JSON."""
    soup = BeautifulSoup(html, "lxml")
    records: list[StatusRecord] = []
    seen: set[int] = set()
    for script in soup.find_all("script"):
        kind = str(script.get("type") or "").lower()
        text = script.string if script.string is not None else script.get_text()
        if not text or not text.strip():
            continue
        if "json" not in kind and not text.strip().startswith(("{", "[")):
            continue
        for record in records_from_json(text.strip()):
            if record.horse_number in seen:
                continue
            seen.add(record.horse_number)
            records.append(record)
    return records


def marks_from_json(body: str, *, source: str, fetched_at: datetime) -> OddsMarks:
    """Same marks when the document is JSON rather than HTML."""
    marks = OddsMarks()
    page_source = as_page_source(source)
    for record in records_from_json(body):
        if record.status == "entered":
            marks.saw_price = True
            continue
        _add_runner(
            marks,
            record.horse_number,
            record.status,
            record.status_label,
            source=page_source,
            fetched_at=fetched_at,
        )
        marks.withheld.add(str(record.horse_number))
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return marks
    _withheld_from_payload(payload, marks)
    return marks


def _marks_from_table(
    table: Tag,
    marks: OddsMarks,
    *,
    source: PageFetchSource,
    fetched_at: datetime,
) -> None:
    rows = [row for row in table.find_all("tr") if isinstance(row, Tag)]
    header_numbers = _header_numbers(rows[0]) if rows else []
    for row in rows:
        cells = [cell for cell in row.find_all(["td", "th"]) if isinstance(cell, Tag)]
        if not cells:
            continue
        combination = _combination_attr(row) or _combination_in_cells(cells)
        status = row_status(row)
        if combination is not None:
            _apply_combination_cells(
                combination,
                cells,
                status,
                marks,
                source=source,
                fetched_at=fetched_at,
            )
            continue
        number = _horse_number_in_row(cells)
        if number is None:
            continue
        price_text = _price_text(cells, number)
        _apply_runner_cell(
            number,
            price_text,
            status,
            marks,
            source=source,
            fetched_at=fetched_at,
        )
        if header_numbers and len(cells) > 1:
            _apply_matrix_row(
                number,
                cells,
                header_numbers,
                marks,
                source=source,
                fetched_at=fetched_at,
            )


def _header_numbers(row: Tag) -> list[int | None]:
    numbers: list[int | None] = []
    for cell in row.find_all(["td", "th"]):
        if not isinstance(cell, Tag):
            continue
        text = compact(cell.get_text(" ", strip=True))
        if _HORSE_NUMBER.fullmatch(text):
            numbers.append(int(text))
        else:
            numbers.append(None)
    if sum(number is not None for number in numbers) < 2:
        return []
    return numbers


def _combination_attr(row: Tag) -> str | None:
    raw = row.get("data-combination")
    if not isinstance(raw, str):
        return None
    return _normalize_combination(raw)


def _combination_in_cells(cells: list[Tag]) -> str | None:
    for cell in cells:
        text = compact(cell.get_text(" ", strip=True))
        normalized = _normalize_combination(text.replace("−", "-").replace("－", "-"))
        if normalized is not None and "-" in normalized:
            return normalized
    return None


def _normalize_combination(text: str) -> str | None:
    token = compact(text)
    if not _COMBINATION.fullmatch(token):
        return None
    numbers = [int(part) for part in token.split("-")]
    if len(set(numbers)) != len(numbers):
        return None
    if numbers != sorted(numbers):
        return None
    return token


def _horse_number_in_row(cells: list[Tag]) -> int | None:
    for cell in cells:
        classes = " ".join(cell.get("class") or [])
        text = compact(cell.get_text(" ", strip=True))
        if "num" in classes or "umaban" in classes.lower() or cell.get("data-horse"):
            if _HORSE_NUMBER.fullmatch(text):
                return int(text)
    for cell in cells[:2]:
        text = compact(cell.get_text(" ", strip=True))
        if _HORSE_NUMBER.fullmatch(text):
            return int(text)
    return None


def _price_text(cells: list[Tag], number: int) -> str:
    for cell in cells:
        classes = " ".join(cell.get("class") or []).lower()
        if any(token in classes for token in ("price", "odds", "tan")):
            return compact(cell.get_text(" ", strip=True))
    texts = [compact(cell.get_text(" ", strip=True)) for cell in cells]
    for text in texts:
        if text == str(number):
            continue
        if status_from_token(text) or is_withheld_token(text) or _positive(text) is not None:
            return text
    return ""


def _apply_runner_cell(
    number: int,
    price_text: str,
    status: tuple[CardRunnerStatus, str] | None,
    marks: OddsMarks,
    *,
    source: PageFetchSource,
    fetched_at: datetime,
) -> None:
    hit = status or status_from_token(price_text)
    if hit is not None and hit[0] in _NON_RUNNER:
        _add_runner(marks, number, hit[0], hit[1], source=source, fetched_at=fetched_at)
        marks.withheld.add(str(number))
        return
    if is_withheld_token(price_text):
        marks.withheld.add(str(number))
        return
    if _positive(price_text) is not None:
        marks.saw_price = True


def _apply_combination_cells(
    combination: str,
    cells: list[Tag],
    status: tuple[CardRunnerStatus, str] | None,
    marks: OddsMarks,
    *,
    source: PageFetchSource,
    fetched_at: datetime,
) -> None:
    texts = [compact(cell.get_text(" ", strip=True)) for cell in cells]
    price_bits = [text for text in texts if text and text != combination]
    hit = status
    if hit is None:
        for text in price_bits:
            hit = status_from_token(text)
            if hit is not None:
                break
    if hit is not None and hit[0] in _NON_RUNNER:
        marks.withheld.add(combination)
        if "-" not in combination:
            _add_runner(marks, int(combination), hit[0], hit[1], source=source, fetched_at=fetched_at)
        return
    if any(is_withheld_token(text) for text in price_bits):
        marks.withheld.add(combination)
        return
    if any(_positive(text) is not None for text in price_bits):
        marks.saw_price = True


def _apply_matrix_row(
    row_number: int,
    cells: list[Tag],
    header_numbers: list[int | None],
    marks: OddsMarks,
    *,
    source: PageFetchSource,
    fetched_at: datetime,
) -> None:
    """A matrix cell is one pair. ``----`` withholds that pair only."""
    del source, fetched_at
    values = [compact(cell.get_text(" ", strip=True)) for cell in cells]
    # The row label occupies the first horse-number cell. Later cells line up
    # with the header when the header has the same width, else with an offset
    # of one label column.
    offset = 0
    if header_numbers and len(values) == len(header_numbers):
        offset = 0
    elif header_numbers and len(values) == len(header_numbers) + 1:
        offset = 1
    else:
        return
    for index, other in enumerate(header_numbers):
        value_index = index + offset
        if other is None or value_index >= len(values) or other == row_number:
            continue
        text = values[value_index]
        left, right = sorted((row_number, other))
        combination = f"{left}-{right}"
        hit = status_from_token(text)
        if hit is not None or is_withheld_token(text):
            marks.withheld.add(combination)
        elif _positive(text) is not None:
            marks.saw_price = True


def _add_runner(
    marks: OddsMarks,
    number: int,
    status: str,
    label: str | None,
    *,
    source: PageFetchSource,
    fetched_at: datetime,
) -> None:
    if status not in _NON_RUNNER or not 1 <= number <= 18:
        return
    code: Literal["scratched", "excluded", "unknown"] = status  # type: ignore[assignment]
    for existing in marks.runners:
        if existing.horse_number == number:
            return
    marks.runners.append(
        NonRunner(
            horse_number=number,
            status=code,
            status_label=label,
            source=source,
            fetched_at=fetched_at,
        )
    )


def _withheld_from_payload(payload: object, marks: OddsMarks) -> None:
    """Combinations a JSON document lists as ``----`` or a scratch label."""
    if not isinstance(payload, dict):
        return
    withheld = payload.get("withheld") or payload.get("void")
    if isinstance(withheld, list):
        for item in withheld:
            combination = _normalize_combination(str(item))
            if combination is not None:
                marks.withheld.add(combination)
    data = payload.get("data")
    if isinstance(data, dict):
        _withheld_from_payload(data, marks)


def _positive(text: str) -> float | None:
    if not text or text in _PLACEHOLDERS or status_from_token(text) or is_withheld_token(text):
        return None
    try:
        number = float(text.replace(",", ""))
    except ValueError:
        return None
    if number <= 0:
        return None
    return number


@dataclass(frozen=True)
class PriceCell:
    """A printed price, or a kept row whose price is null."""

    odds: float | None
    withheld: bool
    non_runner: bool
    status: CardRunnerStatus | None
    status_label: str | None


def read_price_cell(value: object) -> PriceCell | None:
    """Read ``[odds, change, popularity]`` or a scalar.

    ``----`` and a scratch label keep the row with a null price.
    An empty cell or ``**`` drops the row. A number is never invented.
    """
    raw = value[0] if isinstance(value, list | tuple) and value else value
    if isinstance(raw, list | tuple):
        return None
    if isinstance(raw, str):
        text = raw.strip()
        hit = status_from_token(text)
        if hit is not None:
            return PriceCell(None, True, hit[0] in _NON_RUNNER, hit[0], hit[1])
        if is_withheld_token(text):
            return PriceCell(None, True, False, None, None)
        if text in _PLACEHOLDERS:
            return None
        try:
            number = float(text.replace(",", ""))
        except ValueError:
            return None
    elif isinstance(raw, int | float) and not isinstance(raw, bool):
        number = float(raw)
    else:
        return None
    if number <= 0:
        return None
    return PriceCell(number, False, False, None, None)


@dataclass(frozen=True)
class RangeCell:
    """A wide range, or a kept pair whose ends are null."""

    odds_min: float | None
    odds_max: float | None
    withheld: bool
    non_runner: bool
    status_label: str | None


def _non_positive_number(value: object) -> bool:
    """True for ``0`` or a negative change. A blank or a scratch label is not."""
    if isinstance(value, bool) or isinstance(value, list | tuple):
        return False
    if isinstance(value, str):
        text = value.strip()
        if not text or text in _PLACEHOLDERS or status_from_token(text) or is_withheld_token(text):
            return False
        try:
            number = float(text.replace(",", ""))
        except ValueError:
            return False
    elif isinstance(value, int | float):
        number = float(value)
    else:
        return False
    return number <= 0


def _ceiling_point(left: PriceCell | None, raw_right: object) -> bool:
    """The published wide ceiling with a non-positive second field.

    ``9,999.9`` / ``0`` and ``9,999.9`` / ``-2`` are a point range.
    ``2.9`` / ``0.0`` is a missing end and stays dropped.
    """
    if left is None or left.withheld or left.odds is None:
        return False
    if abs(left.odds - _CEILING_ODDS) > _CEILING_TOLERANCE:
        return False
    return _non_positive_number(raw_right)


def read_range_cell(value: object) -> RangeCell | None:
    """Read ``[odds_min, odds_max, popularity]``.

    ``----`` or ``取消`` on either end keeps the pair and nulls both ends.
    One number and one empty end drops the pair. The missing end is not copied.
    The published ceiling ``9,999.9`` with a non-positive second field is both
    ends. A normal one-sided row such as ``2.9`` / ``0.0`` is still dropped.
    """
    if not isinstance(value, list | tuple) or len(value) < 2:
        return None
    left = read_price_cell(value[0])
    right = read_price_cell(value[1])
    if left is not None and left.odds is not None and _ceiling_point(left, value[1]):
        return RangeCell(left.odds, left.odds, False, False, None)
    if left is None or right is None:
        return None
    if left.withheld or right.withheld:
        label = left.status_label or right.status_label
        return RangeCell(
            None,
            None,
            True,
            left.non_runner or right.non_runner,
            label,
        )
    if left.odds is None or right.odds is None:
        return None
    low, high = left.odds, right.odds
    if low > high:
        low, high = high, low
    return RangeCell(low, high, False, False, None)


def non_runners_from_cells(
    cells: Iterable[tuple[str, PriceCell | RangeCell]],
    *,
    source: str,
    fetched_at: datetime,
) -> list[NonRunner]:
    """Horse numbers a win cell marked as scratched or excluded.

    A combination cell does not name which horse. Those horses are not added.
    """
    page_source = as_page_source(source)
    runners: list[NonRunner] = []
    seen: set[int] = set()
    for combination, cell in cells:
        if "-" in combination or not isinstance(cell, PriceCell):
            continue
        if cell.status not in _NON_RUNNER or not combination.isdigit():
            continue
        number = int(combination)
        if number in seen:
            continue
        seen.add(number)
        code: Literal["scratched", "excluded", "unknown"] = cell.status  # type: ignore[assignment]
        runners.append(
            NonRunner(
                horse_number=number,
                status=code,
                status_label=cell.status_label,
                source=page_source,
                fetched_at=fetched_at,
            )
        )
    return runners


def combination_horses(combination: str) -> list[int]:
    """Horse numbers in ``1``, ``3-7``, or ``1-2-5``."""
    horses: list[int] = []
    for part in combination.split("-"):
        if part.isdigit():
            horses.append(int(part))
    return horses


def flag_quotes(
    quotes: list[OddsQuote],
    runners: list[NonRunner] | None,
    withheld: set[str],
    *,
    prefer_runners: bool,
) -> list[OddsQuote]:
    """Mark quotes that include a scratched horse.

    A price stays when the page printed a number. ``----`` or ``取消`` on the
    preferred page, or on the quote itself, keeps the row and nulls the price.
    A win quote for a horse JRA (or the active list) marked scratched is nulled,
    because that page printed a label instead of a price.
    """
    if runners is None and not withheld and not prefer_runners:
        return quotes
    scratched = {
        runner.horse_number
        for runner in runners or []
        if runner.status in _NON_RUNNER
    }
    updated: list[OddsQuote] = []
    for quote in quotes:
        horses = combination_horses(quote.combination)
        includes = any(horse in scratched for horse in horses)
        page_withheld = quote.combination in withheld
        win_scratched = quote.bet_type == "win" and includes
        withheld_flag = quote.withheld or page_withheld or win_scratched
        non_runner = includes or (quote.non_runner and not prefer_runners)
        odds = None if withheld_flag else quote.odds
        odds_min = None if withheld_flag else quote.odds_min
        odds_max = None if withheld_flag else quote.odds_max
        updated.append(
            quote.model_copy(
                update={
                    "non_runner": non_runner,
                    "withheld": withheld_flag,
                    "odds": odds,
                    "odds_min": odds_min,
                    "odds_max": odds_max,
                }
            )
        )
    return updated


@dataclass
class ScratchPage:
    """What one preferred page said about scratches. None means it was not read."""

    runners: list[NonRunner] | None
    withheld: set[str]
    warning: str | None
    blocked: bool = False


def apply_scratch_page(snapshot: RaceOdds, page: ScratchPage) -> RaceOdds:
    """Prefer a read JRA list. A blocked page leaves the netkeiba list in place."""
    warning = join_warnings(snapshot.warning, page.warning)
    if page.blocked or page.runners is None:
        quotes = flag_quotes(snapshot.quotes, snapshot.non_runners, page.withheld, prefer_runners=False)
        return snapshot.model_copy(update={"quotes": quotes, "warning": warning})
    quotes = flag_quotes(snapshot.quotes, page.runners, page.withheld, prefer_runners=True)
    return snapshot.model_copy(
        update={"non_runners": page.runners, "quotes": quotes, "warning": warning}
    )


def merge_non_runners(groups: list[list[NonRunner] | None]) -> list[NonRunner] | None:
    """Union horse numbers. An all-null group stays null."""
    if all(group is None for group in groups):
        return None
    by_number: dict[int, NonRunner] = {}
    for group in groups:
        for runner in group or []:
            by_number.setdefault(runner.horse_number, runner)
    return [by_number[number] for number in sorted(by_number)]


def page_records(body: str) -> list[StatusRecord]:
    """Status objects when the document itself is JSON.

    JSON embedded in HTML is a later fallback page, so this does not scan scripts.
    """
    if not body.lstrip().startswith(("{", "[")):
        return []
    return records_from_json(body)
