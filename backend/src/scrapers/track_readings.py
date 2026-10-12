"""Read cushion, moisture, going, and weather that a track page actually printed.

The hardness scale (12, 10, 8, 7 and 硬め / やや硬め / 標準 / …) is a legend.
It is not the cushion reading. A missing number stays null.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from backend.scrapers.page_fallback import AccessBlocked
from backend.utils.date_utils import parse_measurement_time, parse_post_time

CUSHION_MISSING = "cushion value was not on the page"
GRADE_LABELS = (
    "やや硬め",
    "やや軟らかめ",
    "やや軟らか",
    "軟らかめ",
    "硬め",
    "軟らか",
    "標準",
)
_DATE_TOKEN = re.compile(
    r"\d{4}\s*[/\-年]\s*\d{1,2}\s*[/\-月]\s*\d{1,2}\s*日?"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|\d{1,2}\s*[:時]\s*\d{1,2}\s*分?"
)
_JSON_URL = re.compile(r"""(?:url|URL)\s*:\s*['"]([^'"]+)['"]""")
_SCALE_IDS = frozenset({"cushion_relation"})


@dataclass
class PrintedCushion:
    """Number and grade from one cushion cell. Either may be missing."""

    value: float | None
    grade: str | None
    slot: bool


@dataclass
class PrintedMoisture:
    """Goal and corner moisture copied from one table or list."""

    goal_front: float | None = None
    fourth_corner: float | None = None
    measured_at: datetime | None = None


def join_warnings(*parts: str | None) -> str | None:
    """Join reasons with ``; ``. Empty parts are dropped."""
    items = [part for part in parts if part]
    if not items:
        return None
    return "; ".join(items)


def printed_year(text: str) -> int | None:
    """Year printed as ``2026年``. A page with no year stays unset."""
    match = re.search(r"(\d{4})\s*年", text)
    if match is None:
        return None
    return int(match.group(1))


def printed_float(text: str) -> float | None:
    """A number printed alone in a cell, with an optional percent sign."""
    cleaned = text.strip().replace("%", "").replace("％", "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", cleaned):
        return None
    return float(cleaned)


def parse_cushion_cell(text: str) -> tuple[float | None, str | None]:
    """Split one cushion cell into the printed number and any printed grade.

    Dates and clocks in the same cell are not the cushion value. The hardness
    scale is not passed here. A grade without a number does not invent one.
    """
    grade = next((label for label in GRADE_LABELS if label in text), None)
    cleaned = _DATE_TOKEN.sub(" ", text)
    match = re.search(r"(\d+(?:\.\d+)?)", cleaned)
    if match is None:
        return None, grade
    value = float(match.group(1))
    if not 0 < value < 30:
        return None, grade
    return value, grade


def cushion_from_html(soup: BeautifulSoup | Tag) -> PrintedCushion:
    """Read the cushion slot. An empty slot does not fall through to the scale."""
    for selector in ("#cushion_num", ".CushionData"):
        node = soup.select_one(selector)
        if node is not None:
            value, grade = parse_cushion_cell(node.get_text(" ", strip=True))
            return PrintedCushion(value=value, grade=grade, slot=True)
    for text_node in soup.find_all(string=re.compile("クッション値")):
        parent = text_node.parent
        if not isinstance(parent, Tag) or _in_scale(parent):
            continue
        blob = parent.get_text(" ", strip=True)
        compact = blob.replace(" ", "")
        if compact in {"クッション値", "芝のクッション値"} or len(blob) > 80:
            continue
        value, grade = parse_cushion_cell(blob)
        if value is not None or grade is not None:
            return PrintedCushion(value=value, grade=grade, slot=True)
    return PrintedCushion(value=None, grade=None, slot=False)


def measurement_near(node: Tag | None, year: int | None) -> datetime | None:
    """Clock printed on ``node``. No clock means null."""
    if node is None:
        return None
    return parse_measurement_time(node.get_text(" ", strip=True), year)


def post_time_on_page(text: str) -> str | None:
    """発走時刻 on this document, or null."""
    return parse_post_time(text)


def discover_jra_baba_urls(html: str, page_url: str, fetch: Callable[[str], str]) -> list[str]:
    """Same-site cushion and moisture fragments the baba page's own script calls.

    ``/JRADB/accessJ.html`` and other URLs in that script are not followed.
    A 403 from the script itself propagates as ``AccessBlocked``.
    """
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        absolute = urljoin(page_url, raw.strip())
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            return
        if parsed.netloc != urlparse(page_url).netloc:
            return
        if not parsed.path.endswith(("_data_cushion.html", "_data_moist.html")):
            return
        if absolute in seen:
            return
        seen.add(absolute)
        found.append(absolute)

    for match in _JSON_URL.finditer(html):
        add(match.group(1))
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script", src=True):
        src = urljoin(page_url, str(script.get("src") or ""))
        parsed = urlparse(src)
        if parsed.netloc != urlparse(page_url).netloc:
            continue
        if "/keiba/baba/_js/" not in parsed.path or "slick" in parsed.path:
            continue
        body = fetch(src)
        for match in _JSON_URL.finditer(body):
            add(match.group(1))
    return found


def fetch_or_block(get: Callable[..., str], url: str, *, encoding: str | None) -> str:
    """GET ``url``. HTTP 401/403 becomes ``AccessBlocked`` and stops the chain."""
    try:
        body = get(url, params=None, headers=None, encoding=encoding)
    except AccessBlocked:
        raise
    except Exception as exc:
        status = _status_code(exc)
        if status in {401, 403}:
            message = "returned 403" if status == 403 else "requires login"
            raise AccessBlocked(status, message) from exc
        raise
    if not isinstance(body, str):
        raise TypeError("http client get did not return text")
    return body


def json_object(body: str) -> dict[str, object] | None:
    """Parse a JSON object. HTML and other documents return None."""
    text = body.lstrip()
    if not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def cushion_from_json(payload: dict[str, object]) -> tuple[float | None, str | None]:
    """Copy ``cushion_value`` / ``cushion`` and a printed ``grade``. Do not invent."""
    raw = payload.get("cushion_value", payload.get("cushion"))
    grade_raw = payload.get("grade", payload.get("cushion_grade"))
    grade = grade_raw.strip() if isinstance(grade_raw, str) and grade_raw.strip() else None
    if isinstance(raw, bool) or raw is None:
        return None, grade
    if isinstance(raw, (int, float)):
        value = float(raw)
        if not 0 < value < 30:
            return None, grade
        return value, grade
    if isinstance(raw, str):
        value, cell_grade = parse_cushion_cell(raw)
        return value, grade or cell_grade
    return None, grade


def moisture_pair(payload: dict[str, object], goal_key: str, corner_key: str) -> PrintedMoisture:
    """Copy two moisture numbers when those keys are present."""
    return PrintedMoisture(
        goal_front=_json_float(payload.get(goal_key)),
        fourth_corner=_json_float(payload.get(corner_key)),
    )


def going_label(payload: dict[str, object], key: str) -> str | None:
    """Going printed under ``key``. Anything outside 良/稍重/重/不良 is ignored."""
    raw = payload.get(key)
    if not isinstance(raw, str):
        return None
    label = raw.strip()
    if label in {"良", "稍重", "重", "不良"}:
        return label
    return None


def weather_label(payload: dict[str, object]) -> str | None:
    """Weather string when the JSON has one. Missing stays None."""
    raw = payload.get("weather")
    if not isinstance(raw, str):
        return None
    label = raw.strip()
    return label or None


def _json_float(raw: object) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        return printed_float(raw)
    return None


def _in_scale(tag: Tag) -> bool:
    current: Tag | None = tag
    while current is not None:
        if current.get("id") in _SCALE_IDS:
            return True
        classes = [str(name) for name in (current.get("class") or [])]
        joined = " ".join(classes)
        if "CushionBar" in joined or "CushionType" in joined or "cushion_type" in joined:
            return True
        if "bar" in classes or "break" in classes:
            return True
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return False


def _status_code(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None
