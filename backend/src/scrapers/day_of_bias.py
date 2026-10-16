"""Day-of inside/outside and pace bias from official pre-race pages.

Sources:

- JRA accessD / JRADB shutuba (``https://www.jra.go.jp/JRADB/accessD.html``).
- JRA baba pages (``/keiba/baba/index.html`` and ``index2.html``).

A value is copied only when a heading names that bias and the cell is a short
label. Course geometry (``芝・右 外``), rail position (A/B/C), grass damage,
moisture, cushion, and going are not bias. Odds, popularity, and race names
are not read. No login is required. HTTP 403 is an edge block, not missing
credentials.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from backend.models.race import Race
from backend.scrapers.jra_scraper import JRAScraper
from backend.scrapers.jra_track_scraper import baba_meeting_tab_urls

logger = logging.getLogger(__name__)

TRACK_BIAS_NOT_PUBLISHED = (
    "day-of inside/outside and pace bias not published on accessD or JRA baba"
)
TRACK_BIAS_ACCESSD_EMPTY_BABA_MISSING = (
    "day-of inside/outside and pace bias not published on accessD; "
    "JRA baba has no page for this venue"
)
TRACK_BIAS_ACCESSD_403 = "day-of track bias unavailable: JRA accessD returned 403"
TRACK_BIAS_FETCH_FAILED = "day-of track bias fetch failed"
TRACK_BIAS_PACE_NOT_PUBLISHED = "day-of pace bias not published"
TRACK_BIAS_INSIDE_NOT_PUBLISHED = "day-of inside/outside bias not published"
TRACK_BIAS_DISAGREE = "day-of track bias sources disagree"
TRACK_BIAS_CARD_NOT_ON_MENU = (
    "day-of track bias unavailable: accessD card not on the current menu"
)
TRACK_BIAS_UNMAPPED = "day-of track bias unavailable: race id cannot be mapped to accessD"

BABA_URLS = (
    "https://www.jra.go.jp/keiba/baba/index.html",
    "https://www.jra.go.jp/keiba/baba/index2.html",
)
BABA_ENCODING = "cp932"

_INSIDE_HEADINGS = frozenset({"内外", "内外バイアス", "枠バイアス"})
_PACE_HEADINGS = frozenset({"ペース", "ペースバイアス", "展開バイアス"})
_REJECT_FRAGMENTS = ("傷み", "内柵", "メートル", "コース", "回り")
_MAX_LABEL_CHARS = 16
_WHITESPACE = re.compile(r"\s+")
_BABA_TITLE = re.compile(r"馬場情報（(.+?)競馬場）")


@dataclass(frozen=True)
class BiasLabels:
    """Labels printed under an explicit bias heading. Absent means None."""

    inside_outside: str | None = None
    pace_bias: str | None = None


@dataclass(frozen=True)
class DayOfBiasPublication:
    """What the official pages published for one race. Conditions are separate."""

    inside_outside: str | None
    pace_bias: str | None
    source: str | None
    warning: str | None
    data_sufficiency: str


def not_loaded_publication() -> DayOfBiasPublication:
    """Lookup was not run. Callers keep the card's not-loaded warning."""
    from backend.models.race import TRACK_BIAS_NOT_LOADED

    return DayOfBiasPublication(
        inside_outside=None,
        pace_bias=None,
        source=None,
        warning=TRACK_BIAS_NOT_LOADED,
        data_sufficiency="unavailable",
    )


def fetch_failed_publication() -> DayOfBiasPublication:
    """Unexpected failure. Labels stay null."""
    return DayOfBiasPublication(
        inside_outside=None,
        pace_bias=None,
        source=None,
        warning=TRACK_BIAS_FETCH_FAILED,
        data_sufficiency="unavailable",
    )


def parse_bias_labels(html: str) -> BiasLabels:
    """Copy short labels under bias headings. Anything else stays null."""
    soup = BeautifulSoup(html or "", "lxml")
    inside: list[str] = []
    pace: list[str] = []
    for unit in soup.select("div.data_list_unit"):
        _collect(
            inside,
            pace,
            _node_text(unit.select_one(".head")),
            _node_text(unit.select_one(".content")),
        )
    for row in soup.find_all("tr"):
        heading = row.find("th")
        value = row.find("td")
        if heading is None or value is None:
            continue
        _collect(inside, pace, _node_text(heading), _node_text(value))
    return BiasLabels(inside_outside=_only(inside), pace_bias=_only(pace))


def assemble_publication(
    accessd_labels: BiasLabels,
    accessd_status: str,
    baba_labels: BiasLabels,
    baba_status: str,
) -> DayOfBiasPublication:
    """Merge two official pages. Disagreement drops both labels."""
    inside, inside_sources, inside_conflict = _pick(
        accessd_labels.inside_outside,
        baba_labels.inside_outside,
    )
    pace, pace_sources, pace_conflict = _pick(
        accessd_labels.pace_bias,
        baba_labels.pace_bias,
    )
    if inside_conflict or pace_conflict:
        return DayOfBiasPublication(
            inside_outside=None,
            pace_bias=None,
            source=None,
            warning=TRACK_BIAS_DISAGREE,
            data_sufficiency="unavailable",
        )

    source = _source(inside_sources, pace_sources)
    if inside and pace:
        return DayOfBiasPublication(
            inside_outside=inside,
            pace_bias=pace,
            source=source,
            warning=None,
            data_sufficiency="sufficient",
        )
    if inside or pace:
        warning = (
            TRACK_BIAS_PACE_NOT_PUBLISHED if inside else TRACK_BIAS_INSIDE_NOT_PUBLISHED
        )
        return DayOfBiasPublication(
            inside_outside=inside,
            pace_bias=pace,
            source=source,
            warning=warning,
            data_sufficiency="thin",
        )
    return DayOfBiasPublication(
        inside_outside=None,
        pace_bias=None,
        source=None,
        warning=_unavailable_warning(accessd_status, baba_status),
        data_sufficiency="unavailable",
    )


class DayOfBiasClient:
    """Fetch accessD and the current JRA baba tabs, then parse labels."""

    def __init__(self, jra_scraper: JRAScraper | None = None):
        """Use ``jra_scraper`` so accessD HTML stays in that scraper's cache."""
        self.jra = jra_scraper or JRAScraper()
        self._baba_cache: dict[str, tuple[str | None, str]] = {}

    def publication_for(self, race: Race) -> DayOfBiasPublication:
        """Return published bias for ``race``. Missing labels stay null."""
        accessd_html, accessd_status = self.jra.get_shutuba_html(race.race_id)
        baba_html, baba_status = self._baba_for_venue(race.venue)
        accessd_labels = BiasLabels()
        if accessd_status == "ok" and accessd_html:
            accessd_labels = parse_bias_labels(accessd_html)
        baba_labels = BiasLabels()
        if baba_status == "ok" and baba_html:
            baba_labels = parse_bias_labels(baba_html)
        return assemble_publication(
            accessd_labels,
            accessd_status,
            baba_labels,
            baba_status,
        )

    def _baba_for_venue(self, venue: str | None) -> tuple[str | None, str]:
        """Return the current baba tab for ``venue``, or a status when it is absent."""
        if not venue or venue not in self.jra.VENUE_CODES:
            return None, "no_page"
        cached = self._baba_cache.get(venue)
        if cached is not None:
            return cached
        result = self._fetch_baba(venue)
        self._baba_cache[venue] = result
        return result

    def _fetch_baba(self, venue: str) -> tuple[str | None, str]:
        """GET the current baba tabs. Direct venue URLs often return 403.

        ``index.html`` and ``index2.html`` come first. When neither title is
        this venue, further ``indexN.html`` links inside ``.kaisai_tab`` are
        followed. Facility pages are not.
        """
        saw_403 = False
        saw_error = False
        pages: list[tuple[str, str]] = []
        queue = list(BABA_URLS)
        seen: set[str] = set()
        expanded = False
        while queue:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                html = self.jra.http_client.get(url, encoding=BABA_ENCODING)
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status == 403:
                    saw_403 = True
                else:
                    saw_error = True
                logger.info("JRA baba fetch failed for %s (%s)", url, e)
                continue
            if _baba_venue(html) == venue:
                return html, "ok"
            pages.append((url, html))
            if expanded or any(known not in seen for known in BABA_URLS):
                continue
            expanded = True
            for page_url, body in pages:
                for extra in baba_meeting_tab_urls(body, page_url):
                    if extra not in seen and extra not in queue:
                        queue.append(extra)
        if saw_error:
            return None, "fetch_failed"
        if saw_403:
            return None, "403"
        return None, "no_page"


def _unavailable_warning(accessd_status: str, baba_status: str) -> str:
    """Name which official page was reached and why no label was copied."""
    if accessd_status == "ok" and baba_status == "ok":
        return TRACK_BIAS_NOT_PUBLISHED
    if accessd_status == "ok" and baba_status == "no_page":
        return TRACK_BIAS_ACCESSD_EMPTY_BABA_MISSING
    if accessd_status == "403":
        return _with_baba_clause(TRACK_BIAS_ACCESSD_403, baba_status)
    if accessd_status == "not_found":
        return _with_baba_clause(TRACK_BIAS_CARD_NOT_ON_MENU, baba_status)
    if accessd_status == "unmapped":
        return _with_baba_clause(TRACK_BIAS_UNMAPPED, baba_status)
    if accessd_status == "fetch_failed" and baba_status in {"fetch_failed", "403"}:
        return TRACK_BIAS_FETCH_FAILED
    base = TRACK_BIAS_FETCH_FAILED
    if accessd_status == "ok":
        base = "day-of inside/outside and pace bias not published on accessD"
    return _with_baba_clause(base, baba_status)


def _with_baba_clause(base: str, baba_status: str) -> str:
    """Append what the baba tabs did, when that changes the reason."""
    if baba_status == "ok":
        return base + "; JRA baba did not publish inside/outside or pace bias"
    if baba_status == "no_page":
        return base + "; JRA baba has no page for this venue"
    if baba_status == "403":
        return base + "; JRA baba returned 403"
    if baba_status == "fetch_failed":
        return base + "; JRA baba fetch failed"
    return base


def _pick(
    accessd_value: str | None,
    baba_value: str | None,
) -> tuple[str | None, set[str], bool]:
    """Choose one published string. Two different strings are a conflict."""
    if accessd_value and baba_value and accessd_value != baba_value:
        return None, set(), True
    sources: set[str] = set()
    value = accessd_value or baba_value
    if accessd_value:
        sources.add("accessd")
    if baba_value:
        sources.add("jra_baba")
    return value, sources, False


def _source(inside_sources: set[str], pace_sources: set[str]) -> str | None:
    """``accessd``, ``jra_baba``, or both when each page contributed a label."""
    sources = inside_sources | pace_sources
    if sources == {"accessd", "jra_baba"}:
        return "accessd+jra_baba"
    if "accessd" in sources:
        return "accessd"
    if "jra_baba" in sources:
        return "jra_baba"
    return None


def _collect(inside: list[str], pace: list[str], heading: str, content: str) -> None:
    """Append a cell when the heading is a bias field and the text is a label."""
    key = _WHITESPACE.sub("", heading)
    label = _label(content)
    if label is None:
        return
    if key in _INSIDE_HEADINGS:
        inside.append(label)
    elif key in _PACE_HEADINGS:
        pace.append(label)


def _label(content: str) -> str | None:
    """Return a short published label, or None when the cell is a note."""
    text = _WHITESPACE.sub(" ", content).strip()
    if not text or len(text) > _MAX_LABEL_CHARS:
        return None
    if any(fragment in text for fragment in _REJECT_FRAGMENTS):
        return None
    return text


def _only(values: list[str]) -> str | None:
    """Use the label when every copy on the page is the same."""
    unique = list(dict.fromkeys(values))
    if len(unique) == 1:
        return unique[0]
    return None


def _node_text(node) -> str:
    """Visible text of a node, or empty when the node is missing."""
    if node is None:
        return ""
    return node.get_text(" ", strip=True)


def _baba_venue(html: str) -> str | None:
    """Racecourse named in the baba page title. Nav links are ignored."""
    soup = BeautifulSoup(html or "", "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    match = _BABA_TITLE.search(title)
    if match:
        return match.group(1)
    return None
