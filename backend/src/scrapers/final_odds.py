"""Final odds from a public page.

A price is final only when the odds heading or table says 確定オッズ or
最終オッズ, or the page is a post-race result (not 確定前). A menu, button,
or nav label is not that mark. Live and yoso numbers are not copied.
Missing prices stay null. Wide ranges need both ends. Place may be a single
price or a printed range. ``---.-`` is an empty cell, not a price.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from backend.models.odds import BetType, NonRunner, OddsQuote, OddsSource, PageFetchSource
from backend.models.race import RacePayout, SettledFinisher, SettledRaceResult
from backend.scrapers.page_fallback import FetchedBody
from backend.scrapers.runner_status import (
    PriceCell,
    as_page_source,
    non_runners_from_cells,
    read_popularity,
    read_price_cell,
    read_range_cell,
)
from backend.utils.date_utils import tokyo_now

FINAL_ODDS_NOT_CONFIRMED = "final odds were not confirmed"
FINAL_ODDS_NOT_ON_PAGE = "final odds were not on the page"
PLACE_ODDS_MISSING = "place odds were not on the page"
COMBO_ODDS_MISSING = "combination odds were not on the page"
FINAL_ODDS_NOT_SETTLED = "final odds require a confirmed complete race"
PAYOUT_DISAGREE = "final odds and payout disagree"
# JRA pays in 10 yen. odds × 100 within that unit is the same price.
_PAYOUT_ROUNDING_YEN = 10

_FINAL_MARKS = ("確定オッズ", "最終オッズ")
# The odds heading and the table. A menu button can say the same words.
_FINAL_HEADINGS = ("title", "h1", "h2", "h3", "h4", "caption", "th")
_FINAL_CHROME = frozenset({"a", "button", "nav"})
_PAYOUT_BET = {"ワイド": "wide", "馬連": "umaren", "3連複": "sanrenpuku", "三連複": "sanrenpuku"}
_DATED = re.compile(r"(\d{4})[-/](\d{2})[-/](\d{2})")
_DATED_COMPACT = re.compile(r"(\d{4})(\d{2})(\d{2})")
_JSON_URL = re.compile(r"""(?:url|URL)\s*:\s*['"]([^'"]+)['"]""")
_RANGE_SPLIT = re.compile(r"[-–−－〜～]")
_HORSE = re.compile(r"^(?:[1-9]|1[0-8])$")
_SECTION = {
    "単勝": "win",
    "複勝": "place",
    "馬連": "umaren",
    "ワイド": "wide",
    "3連複": "sanrenpuku",
    "三連複": "sanrenpuku",
}
_JSON_KEYS = {
    "win": "win",
    "place": "place",
    "umaren": "umaren",
    "wide": "wide",
    "sanrenpuku": "sanrenpuku",
    "単勝": "win",
    "複勝": "place",
    "馬連": "umaren",
    "ワイド": "wide",
    "3連複": "sanrenpuku",
    "三連複": "sanrenpuku",
}


@dataclass
class PublicOddsPages:
    """Odds documents collected before the chain stopped."""

    pages: list[FetchedBody] = field(default_factory=list)
    warning: str | None = None
    blocked: bool = False


@dataclass
class FinalOddsParse:
    """Quotes taken from pages that said the odds were final."""

    confirmed: bool
    quotes: list[OddsQuote] = field(default_factory=list)
    non_runners: list[NonRunner] = field(default_factory=list)
    source: str | None = None
    warning: str | None = None


def page_says_final_odds(text: str) -> bool:
    """True for 確定オッズ or 最終オッズ in the odds heading or table.

    A plain label is the whole string, so ``確 定 オッズ`` still matches and
    ``結果確定`` does not. In HTML the words count only in ``title``, ``h1``
    to ``h4``, ``caption``, or ``th``. A menu, a button, a nav link, or a
    link wrapped inside a heading does not make the page final.
    """
    if "<" not in text:
        compact = re.sub(r"\s+", "", text)
        return any(mark in compact for mark in _FINAL_MARKS)
    soup = BeautifulSoup(text, "lxml")
    for tag in soup.find_all(_FINAL_HEADINGS):
        if not isinstance(tag, Tag) or _inside_chrome(tag):
            continue
        compact = re.sub(r"\s+", "", _unlinked_text(tag))
        if any(mark in compact for mark in _FINAL_MARKS):
            return True
    return False


def _inside_chrome(tag: Tag) -> bool:
    """True when a heading sits inside a link, a button, or navigation."""
    for parent in tag.parents:
        if getattr(parent, "name", None) in _FINAL_CHROME:
            return True
    return False


def _unlinked_text(tag: Tag) -> str:
    """Heading text, leaving out words that belong to a link or a button."""
    parts: list[str] = []
    for child in tag.descendants:
        if not isinstance(child, str):
            continue
        parent = child.parent
        if isinstance(parent, Tag) and _chrome_between(parent, tag):
            continue
        parts.append(child)
    return "".join(parts)


def _chrome_between(node: Tag, stop: Tag) -> bool:
    current: Tag | None = node
    while current is not None and current is not stop:
        if current.name in _FINAL_CHROME:
            return True
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return False


def priced_without_final_label(html: str) -> bool:
    """True when a document prints a price and does not say the odds are final.

    Those numbers are a live board. A later render must not replace them.
    ``---.-`` is not a price.
    """
    if page_says_final_odds(html) or _json_says_final(html):
        return False
    return _has_price_text(html)


def result_is_post_race(settled: SettledRaceResult) -> bool:
    """A finish table is post-race. 確定前 is not a final-odds page."""
    if settled.result_phase == "provisional":
        return False
    if not settled.results:
        return False
    warning = settled.warning or ""
    if warning in {"race not yet settled", "race result not found", "race result fetch failed"}:
        return False
    return True


def final_odds_need_fallback(html: str) -> bool:
    """Keep walking only when the page has not yet shown a price.

    A live board that prints numbers without 確定オッズ is finished: those
    numbers are not final, and a later render must not be asked to invent them.
    """
    if page_says_final_odds(html) and _has_price_text(html):
        return False
    if _has_price_text(html) and not page_says_final_odds(html) and not _json_says_final(html):
        return False
    return True


def same_site_odds_urls(html: str, page_url: str) -> list[str]:
    """Same-site odds or final JSON the page's own script names.

    Login endpoints and other hosts are left out.
    """
    found: list[str] = []
    seen: set[str] = set()
    page = urlparse(page_url)
    for match in _JSON_URL.finditer(html):
        raw = match.group(1).strip()
        path = raw.split("?", 1)[0].lower()
        if "odds" not in path and "final" not in path and "kakutei" not in path:
            continue
        if "login" in path:
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


def parse_confirmed_odds(
    pages: list[FetchedBody],
    race_id: str,
) -> FinalOddsParse:
    """Read win, place, and combination odds when the chain says they are final.

    An earlier page that says 確定オッズ lets a later same-site JSON fill prices.
    A page that never says 確定オッズ contributes nothing, even if it has numbers.
    """
    confirmed_context = False
    quotes: list[OddsQuote] = []
    runners: list[NonRunner] = []
    sources: list[str] = []
    seen: set[tuple[str, str]] = set()
    for page in pages:
        if page_says_final_odds(page.body) or _json_says_final(page.body):
            confirmed_context = True
        if not confirmed_context:
            continue
        extra_quotes, extra_runners = _parse_document(page, race_id)
        if extra_quotes or extra_runners:
            sources.append(page.source)
        for quote in extra_quotes:
            key = (quote.bet_type, quote.combination)
            if key in seen:
                continue
            seen.add(key)
            quotes.append(quote)
        known = {runner.horse_number for runner in runners}
        for runner in extra_runners:
            if runner.horse_number not in known:
                runners.append(runner)
                known.add(runner.horse_number)
    if not quotes:
        warning = FINAL_ODDS_NOT_ON_PAGE if confirmed_context else None
        return FinalOddsParse(False, [], runners, _join_source(sources), warning)
    return FinalOddsParse(True, quotes, runners, _join_source(sources), None)


def quotes_from_settled_win(settled: SettledRaceResult) -> list[OddsQuote]:
    """Win odds printed on a post-race result table.

    Scratched and excluded rows stay, with a null price. A blank odds cell is
    left out. Place and combination prices are not invented from payouts.
    """
    quotes: list[OddsQuote] = []
    for row in settled.results:
        if row.horse_number is None:
            continue
        source = _as_odds_source(row.source or "netkeiba_html")
        fetched = row.fetched_at or settled.fetched_at
        combination = str(row.horse_number)
        if row.status in {"scratched", "excluded"}:
            quotes.append(
                _quote(
                    race_id=settled.race_id,
                    bet_type="win",
                    combination=combination,
                    odds=None,
                    odds_min=None,
                    odds_max=None,
                    source=source,
                    fetched_at=fetched,
                    non_runner=True,
                    withheld=True,
                )
            )
            continue
        if row.odds is None:
            continue
        quotes.append(
            _quote(
                race_id=settled.race_id,
                bet_type="win",
                combination=combination,
                odds=row.odds,
                odds_min=None,
                odds_max=None,
                source=source,
                fetched_at=fetched,
                non_runner=False,
                withheld=False,
            )
        )
    return quotes


def non_runners_from_finishers(finishers: list[SettledFinisher]) -> list[NonRunner]:
    """Scratches and exclusions on the result page. Other statuses are not added."""
    runners: list[NonRunner] = []
    for row in finishers:
        if row.horse_number is None or row.status not in {"scratched", "excluded"}:
            continue
        runners.append(
            NonRunner(
                horse_number=row.horse_number,
                status=row.status,
                status_label=row.status_label,
                source=_page_source(row.source),
                fetched_at=row.fetched_at,
            )
        )
    return runners


def canonical_combination(text: str, bet_type: str) -> str | None:
    """``3-7`` or ``1-2-5`` in ascending horse-number order. Anything else is null."""
    return _normalize_combo(text, bet_type)


def payout_matches_quote(payout: int, quote: OddsQuote) -> bool | None:
    """True when payout yen is odds × 100, within the 10 yen unit.

    Wide compares the payout with the printed range. None when this quote has
    no price to compare. The payout is not turned into an odds value.
    """
    if quote.withheld:
        return None
    if quote.bet_type == "wide":
        if quote.odds_min is None or quote.odds_max is None:
            return None
        low = quote.odds_min * 100 - _PAYOUT_ROUNDING_YEN
        high = quote.odds_max * 100 + _PAYOUT_ROUNDING_YEN
        return low <= payout <= high
    if quote.odds is None:
        return None
    return abs(payout - quote.odds * 100) <= _PAYOUT_ROUNDING_YEN


def payout_disagreements(
    quotes: list[OddsQuote],
    payouts: list[RacePayout],
) -> list[str]:
    """Winning tickets whose yen is not the printed final odds.

    A missing quote is not a disagreement. The caller already warns that the
    market was not on the page. This does not replace either number.
    """
    by_key = {(quote.bet_type, quote.combination): quote for quote in quotes}
    notes: list[str] = []
    for row in payouts:
        bet_type = _PAYOUT_BET.get(row.bet_type.strip())
        if bet_type is None:
            continue
        combination = canonical_combination(row.combination, bet_type)
        if combination is None:
            continue
        quote = by_key.get((bet_type, combination))
        if quote is None:
            continue
        matched = payout_matches_quote(row.payout, quote)
        if matched is None or matched:
            continue
        note = f"{PAYOUT_DISAGREE}: {bet_type} {combination}"
        if note not in notes:
            notes.append(note)
    return notes


def odds_api_is_final_payload(body: str) -> bool:
    """True when this odds API body is ``result`` and the odds map is non-empty.

    An empty ``data`` string and an empty odds map are not final prices.
    ``middle`` and ``yoso`` are not final. This does not decide caching.
    """
    payload = _json_object(body)
    if payload is None or str(payload.get("status") or "").strip().lower() != "result":
        return False
    return _api_odds_nonempty(payload)


def odds_api_is_replayable(body: str, *, today: date | None = None) -> bool:
    """True when a final odds API body may be stored.

    Today or a later ``official_datetime`` in Asia/Tokyo is not stored.
    A result payload with prices and no datetime may be stored. The caller
    that already knows the race day is today skips the cache itself.
    """
    if not odds_api_is_final_payload(body):
        return False
    payload = _json_object(body)
    if payload is None:
        return False
    current = today or tokyo_now().date()
    official = _official_day(payload)
    if official is not None and official >= current:
        return False
    return True


def quotes_from_result_api(
    page: FetchedBody,
    race_id: str,
    bet_type: BetType,
) -> list[OddsQuote]:
    """Quotes from a netkeiba odds API body the page itself loaded.

    Only ``status`` ``result`` with a non-empty odds map. A pre-race payload
    contributes nothing. The quote ``source`` stays the fetch that loaded the
    body. Popularity is the third field when that field is a rank.
    """
    if not odds_api_is_final_payload(page.body):
        return []
    payload = _json_object(page.body)
    if payload is None:
        return []
    from backend.scrapers.netkeiba_odds_scraper import (
        parse_netkeiba_sanrenpuku_odds,
        parse_netkeiba_umaren_odds,
        parse_netkeiba_wide_odds,
    )

    parsers = {
        "wide": parse_netkeiba_wide_odds,
        "umaren": parse_netkeiba_umaren_odds,
        "sanrenpuku": parse_netkeiba_sanrenpuku_odds,
    }
    parser = parsers.get(bet_type)
    if parser is None:
        return []
    snapshot = parser(payload, race_id, fetched_at=page.fetched_at)
    if snapshot.race_phase != "post":
        return []
    tagged = _as_odds_source(page.source)
    quotes: list[OddsQuote] = []
    for quote in snapshot.quotes:
        if quote.bet_type != bet_type:
            continue
        if quote.source != tagged:
            quote = quote.model_copy(update={"source": tagged})
        quotes.append(quote)
    return quotes


def missing_market_warning(quotes: list[OddsQuote] | None) -> str | None:
    """Warn for place or combination markets the confirmed pages did not print."""
    if not quotes:
        return None
    kinds = {quote.bet_type for quote in quotes}
    notes: list[str] = []
    if "place" not in kinds:
        notes.append(PLACE_ODDS_MISSING)
    missing = [name for name in ("umaren", "wide", "sanrenpuku") if name not in kinds]
    if missing:
        notes.append(f"{COMBO_ODDS_MISSING}: {', '.join(missing)}")
    if not notes:
        return None
    return "; ".join(notes)


def _parse_document(page: FetchedBody, race_id: str) -> tuple[list[OddsQuote], list[NonRunner]]:
    body = page.body.strip()
    if body.startswith("{") or body.startswith("["):
        return _parse_json_text(body, race_id, page.source, page.fetched_at)
    soup = BeautifulSoup(body, "lxml")
    quotes: list[OddsQuote] = []
    runners: list[NonRunner] = []
    for script in soup.find_all("script"):
        text = script.string if script.string is not None else script.get_text()
        if not text:
            continue
        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            extra_quotes, extra_runners = _parse_json_text(
                stripped, race_id, _script_source(page.source), page.fetched_at
            )
            quotes.extend(extra_quotes)
            runners.extend(extra_runners)
    table_quotes, table_runners = _parse_html_tables(soup, race_id, page.source, page.fetched_at)
    quotes.extend(table_quotes)
    runners.extend(table_runners)
    return quotes, runners


def _parse_html_tables(
    soup: BeautifulSoup,
    race_id: str,
    source: str,
    fetched_at: datetime,
) -> tuple[list[OddsQuote], list[NonRunner]]:
    odds_source = _as_odds_source(source)
    quotes: list[OddsQuote] = []
    priced: list[tuple[str, PriceCell]] = []
    for table in soup.find_all("table"):
        if not isinstance(table, Tag):
            continue
        section = _table_section(table)
        rows = [row for row in table.find_all("tr") if isinstance(row, Tag)]
        if section == "win_place":
            for row in rows:
                built, cell = _win_place_row(row, race_id, odds_source, fetched_at)
                quotes.extend(built)
                if cell is not None:
                    priced.append(cell)
            continue
        if section is None:
            continue
        for row in rows:
            quote = _combination_row(row, section, race_id, odds_source, fetched_at)
            if quote is not None:
                quotes.append(quote)
    runners = non_runners_from_cells(priced, source=source, fetched_at=fetched_at)
    return quotes, runners


def _table_section(table: Tag) -> str | None:
    token = _compact(table.get("id") or "")
    caption = table.find("caption")
    caption_text = _compact(caption.get_text()) if isinstance(caption, Tag) else ""
    heading = ""
    previous = table.find_previous(["h1", "h2", "h3", "h4"])
    if isinstance(previous, Tag):
        heading = _compact(previous.get_text())
    blob = f"{token} {caption_text} {heading}"
    if "単勝" in blob and "複勝" in blob:
        return "win_place"
    header_cells = [
        _compact(cell.get_text()) for cell in table.find_all("th") if isinstance(cell, Tag)
    ]
    if "単勝" in header_cells and "複勝" in header_cells:
        return "win_place"
    for label, kind in _SECTION.items():
        if label in blob or label in header_cells:
            return kind
    identity = (table.get("id") or "") if isinstance(table.get("id"), str) else ""
    if identity in {"win_place", "umaren", "wide", "sanrenpuku"}:
        return "win_place" if identity == "win_place" else identity
    return None


def _win_place_row(
    row: Tag,
    race_id: str,
    source: OddsSource,
    fetched_at: datetime,
) -> tuple[list[OddsQuote], tuple[str, PriceCell] | None]:
    cells = [cell for cell in row.find_all(["td", "th"]) if isinstance(cell, Tag)]
    if len(cells) < 2:
        return [], None
    number = _horse_in_row(cells)
    if number is None:
        return [], None
    win_text = _cell_by_class(cells, ("tan", "win")) or _compact(cells[1].get_text())
    place_text = ""
    if len(cells) >= 3:
        place_text = _cell_by_class(cells, ("fuku", "place")) or _compact(cells[2].get_text())
    quotes: list[OddsQuote] = []
    win_cell = read_price_cell(win_text)
    priced: tuple[str, PriceCell] | None = None
    if win_cell is not None:
        quotes.append(
            _quote(
                race_id=race_id,
                bet_type="win",
                combination=number,
                odds=None if win_cell.withheld else win_cell.odds,
                odds_min=None,
                odds_max=None,
                source=source,
                fetched_at=fetched_at,
                non_runner=win_cell.non_runner,
                withheld=win_cell.withheld,
            )
        )
        priced = (number, win_cell)
    if place_text:
        place = _place_quote(place_text, race_id, number, source, fetched_at, win_cell)
        if place is not None:
            quotes.append(place)
    return quotes, priced


def _place_quote(
    text: str,
    race_id: str,
    combination: str,
    source: OddsSource,
    fetched_at: datetime,
    win_cell: PriceCell | None,
) -> OddsQuote | None:
    non_runner = bool(win_cell and win_cell.non_runner)
    parts = [part for part in _RANGE_SPLIT.split(text) if part]
    if len(parts) == 2:
        left = read_price_cell(parts[0])
        right = read_price_cell(parts[1])
        if left is None or right is None:
            return None
        if left.withheld or right.withheld:
            return _quote(
                race_id=race_id,
                bet_type="place",
                combination=combination,
                odds=None,
                odds_min=None,
                odds_max=None,
                source=source,
                fetched_at=fetched_at,
                non_runner=non_runner or left.non_runner or right.non_runner,
                withheld=True,
            )
        if left.odds is None or right.odds is None:
            return None
        low, high = left.odds, right.odds
        if low > high:
            low, high = high, low
        return _quote(
            race_id=race_id,
            bet_type="place",
            combination=combination,
            odds=None,
            odds_min=low,
            odds_max=high,
            source=source,
            fetched_at=fetched_at,
            non_runner=non_runner,
            withheld=False,
        )
    cell = read_price_cell(text)
    if cell is None:
        return None
    return _quote(
        race_id=race_id,
        bet_type="place",
        combination=combination,
        odds=None if cell.withheld else cell.odds,
        odds_min=None,
        odds_max=None,
        source=source,
        fetched_at=fetched_at,
        non_runner=non_runner or cell.non_runner,
        withheld=cell.withheld,
    )


def _combination_row(
    row: Tag,
    section: str,
    race_id: str,
    source: OddsSource,
    fetched_at: datetime,
) -> OddsQuote | None:
    if section not in {"umaren", "wide", "sanrenpuku"}:
        return None
    cells = [cell for cell in row.find_all(["td", "th"]) if isinstance(cell, Tag)]
    raw = row.get("data-combination")
    combination = _normalize_combo(raw if isinstance(raw, str) else "", section)
    price_text = ""
    for cell in cells:
        text = _compact(cell.get_text())
        if combination is None:
            guessed = _normalize_combo(text, section)
            if guessed is not None:
                combination = guessed
                continue
        if text and text != combination:
            price_text = text
    if combination is None or not price_text:
        return None
    popularity = _row_popularity(row, cells)
    if section == "wide":
        return _wide_quote(price_text, race_id, combination, source, fetched_at, popularity)
    price = read_price_cell(price_text)
    if price is None:
        return None
    return _quote(
        race_id=race_id,
        bet_type=section,  # type: ignore[arg-type]
        combination=combination,
        odds=None if price.withheld else price.odds,
        odds_min=None,
        odds_max=None,
        popularity=popularity,
        source=source,
        fetched_at=fetched_at,
        non_runner=price.non_runner,
        withheld=price.withheld,
    )


def _wide_quote(
    text: str,
    race_id: str,
    combination: str,
    source: OddsSource,
    fetched_at: datetime,
    popularity: int | None = None,
) -> OddsQuote | None:
    parts = [part for part in _RANGE_SPLIT.split(text) if part]
    if len(parts) != 2:
        cell = read_price_cell(text)
        if cell is not None and cell.withheld:
            return _quote(
                race_id=race_id,
                bet_type="wide",
                combination=combination,
                odds=None,
                odds_min=None,
                odds_max=None,
                popularity=popularity,
                source=source,
                fetched_at=fetched_at,
                non_runner=cell.non_runner,
                withheld=True,
            )
        return None
    span = read_range_cell([parts[0], parts[1]])
    if span is None:
        return None
    return _quote(
        race_id=race_id,
        bet_type="wide",
        combination=combination,
        odds=None,
        odds_min=span.odds_min,
        odds_max=span.odds_max,
        popularity=popularity,
        source=source,
        fetched_at=fetched_at,
        non_runner=span.non_runner,
        withheld=span.withheld,
    )


def _parse_json_text(
    text: str,
    race_id: str,
    source: str,
    fetched_at: datetime,
) -> tuple[list[OddsQuote], list[NonRunner]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [], []
    if not isinstance(payload, dict):
        return [], []
    odds_source = _as_odds_source(source)
    quotes: list[OddsQuote] = []
    priced: list[tuple[str, PriceCell]] = []
    markets = payload.get("odds") if isinstance(payload.get("odds"), dict) else payload
    if not isinstance(markets, dict):
        return [], []
    for raw_key, kind in _JSON_KEYS.items():
        block = markets.get(raw_key)
        if not isinstance(block, dict):
            continue
        for combo_key, value in block.items():
            built, cell = _json_quote(kind, str(combo_key), value, race_id, odds_source, fetched_at)
            if built is not None:
                quotes.append(built)
            if cell is not None:
                priced.append(cell)
    runners = non_runners_from_cells(priced, source=source, fetched_at=fetched_at)
    return quotes, runners


def _json_quote(
    kind: str,
    combo_key: str,
    value: object,
    race_id: str,
    source: OddsSource,
    fetched_at: datetime,
) -> tuple[OddsQuote | None, tuple[str, PriceCell] | None]:
    if kind in {"win", "place"}:
        combination = _horse_combination(combo_key)
    else:
        combination = _normalize_combo(combo_key.replace("−", "-"), kind)
    if combination is None:
        return None, None
    popularity = read_popularity(value)
    if kind == "wide" or (kind == "place" and isinstance(value, list)):
        span = read_range_cell(value)
        if span is None and isinstance(value, str):
            return _text_market_quote(kind, value, race_id, combination, source, fetched_at)
        if span is None:
            return None, None
        quote = _quote(
            race_id=race_id,
            bet_type=kind,  # type: ignore[arg-type]
            combination=combination,
            odds=None,
            odds_min=span.odds_min,
            odds_max=span.odds_max,
            popularity=None if kind == "place" else popularity,
            source=source,
            fetched_at=fetched_at,
            non_runner=span.non_runner,
            withheld=span.withheld,
        )
        return quote, None
    if isinstance(value, list):
        cell = read_price_cell(value)
    else:
        cell = read_price_cell(value)
    if cell is None and isinstance(value, str) and kind == "place":
        return _text_market_quote(kind, value, race_id, combination, source, fetched_at)
    if cell is None:
        return None, None
    quote = _quote(
        race_id=race_id,
        bet_type=kind,  # type: ignore[arg-type]
        combination=combination,
        odds=None if cell.withheld else cell.odds,
        odds_min=None,
        odds_max=None,
        popularity=popularity,
        source=source,
        fetched_at=fetched_at,
        non_runner=cell.non_runner,
        withheld=cell.withheld,
    )
    priced = (combination, cell) if kind == "win" else None
    return quote, priced


def _text_market_quote(
    kind: str,
    text: str,
    race_id: str,
    combination: str,
    source: OddsSource,
    fetched_at: datetime,
) -> tuple[OddsQuote | None, tuple[str, PriceCell] | None]:
    if kind == "place":
        quote = _place_quote(text, race_id, combination, source, fetched_at, None)
        return quote, None
    if kind == "wide":
        quote = _wide_quote(text, race_id, combination, source, fetched_at)
        return quote, None
    return None, None


def _quote(
    *,
    race_id: str,
    bet_type: BetType,
    combination: str,
    odds: float | None,
    odds_min: float | None,
    odds_max: float | None,
    source: OddsSource,
    fetched_at: datetime,
    non_runner: bool,
    withheld: bool,
    popularity: int | None = None,
) -> OddsQuote:
    return OddsQuote(
        race_id=race_id,
        bet_type=bet_type,
        combination=combination,
        odds=odds,
        odds_min=odds_min,
        odds_max=odds_max,
        popularity=popularity,
        source=source,
        kind="live",
        fetched_at=fetched_at,
        market_updated_at=None,
        non_runner=non_runner,
        withheld=withheld,
    )


def _json_says_final(text: str) -> bool:
    stripped = text.strip()
    if not stripped.startswith("{"):
        return False
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    label = str(payload.get("label") or payload.get("title") or "")
    return page_says_final_odds(label)


def _has_price_text(html: str) -> bool:
    """True when a cell or a final JSON body actually prints a price.

    ``---.-`` and ``----`` are not prices. A range counts only when both ends
    are numbers. A decimal anywhere in a script is not a price.
    """
    stripped = html.strip()
    if _json_says_final(stripped) and re.search(r"\d+\.\d+", stripped):
        return True
    soup = BeautifulSoup(html, "lxml")
    for cell in soup.find_all(["td", "span"]):
        if not isinstance(cell, Tag):
            continue
        classes = " ".join(cell.get("class") or []).lower()
        text = _compact(cell.get_text())
        if not text:
            continue
        interesting = any(token in classes for token in ("odds", "tan", "fuku", "price"))
        if cell.get("data-combination") or interesting:
            if _cell_is_price(text):
                return True
    return False


def _cell_is_price(text: str) -> bool:
    """A number, or a range with two numbers. Placeholders and ``----`` are not."""
    cell = read_price_cell(text)
    if cell is not None and not cell.withheld and cell.odds is not None:
        return True
    parts = [part for part in _RANGE_SPLIT.split(text) if part]
    if len(parts) < 2:
        return False
    ranged = read_range_cell(parts[:2])
    return ranged is not None and ranged.odds_min is not None


def _horse_in_row(cells: list[Tag]) -> str | None:
    for cell in cells:
        classes = " ".join(cell.get("class") or []).lower()
        text = _compact(cell.get_text())
        if "num" in classes or cell.get("data-horse"):
            number = _horse_combination(text)
            if number is not None:
                return number
    for cell in cells[:2]:
        number = _horse_combination(_compact(cell.get_text()))
        if number is not None:
            return number
    return None


def _cell_by_class(cells: list[Tag], names: tuple[str, ...]) -> str:
    for cell in cells:
        classes = " ".join(cell.get("class") or []).lower()
        if any(name in classes for name in names):
            return _compact(cell.get_text())
    return ""


def _horse_combination(value: str) -> str | None:
    token = _compact(value).lstrip("0") or "0"
    if _HORSE.fullmatch(token) is None:
        return None
    return token


def _normalize_combo(text: str, section: str) -> str | None:
    token = _compact(text).replace("−", "-").replace("－", "-").replace("–", "-")
    parts = token.split("-")
    if any(_HORSE.fullmatch(part) is None for part in parts):
        return None
    numbers = [int(part) for part in parts]
    if len(set(numbers)) != len(numbers):
        return None
    if section == "sanrenpuku" and len(numbers) != 3:
        return None
    if section in {"umaren", "wide"} and len(numbers) != 2:
        return None
    ordered = sorted(numbers)
    return "-".join(str(number) for number in ordered)


def _as_odds_source(source: str) -> OddsSource:
    allowed: set[str] = {
        "netkeiba_yoso",
        "netkeiba_live",
        "jradb",
        "jra_html",
        "jra_js",
        "netkeiba_html",
        "netkeiba_js",
        "headless",
    }
    if source in allowed:
        return source  # type: ignore[return-value]
    if source.startswith("jra"):
        return "jra_html"
    return "netkeiba_html"


def _script_source(page_source: str) -> str:
    if page_source == "jra_html":
        return "jra_js"
    if page_source == "netkeiba_html":
        return "netkeiba_js"
    return page_source


def _page_source(source: str | None) -> PageFetchSource | None:
    if source is None:
        return None
    return as_page_source(source)


def _join_source(sources: list[str]) -> str | None:
    found: list[str] = []
    for source in sources:
        if source and source not in found:
            found.append(source)
    if not found:
        return None
    return "+".join(found)


def _compact(text: object) -> str:
    return re.sub(r"\s+", "", str(text))


def _row_popularity(row: Tag, cells: list[Tag]) -> int | None:
    """Rank printed on the row. A blank cell stays null."""
    raw = row.get("data-popularity")
    if isinstance(raw, str):
        rank = read_popularity(raw)
        if rank is not None:
            return rank
    for cell in cells:
        classes = " ".join(cell.get("class") or []).lower()
        if "ninki" in classes or "popular" in classes:
            rank = read_popularity(_compact(cell.get_text()))
            if rank is not None:
                return rank
    return None


def _json_object(body: str) -> dict[str, object] | None:
    text = body.strip()
    if not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _api_odds_nonempty(payload: dict[str, object]) -> bool:
    """True when ``data.odds`` has at least one combination."""
    data = payload.get("data")
    if not isinstance(data, dict):
        return False
    odds = data.get("odds")
    if not isinstance(odds, dict) or not odds:
        return False
    for block in odds.values():
        if isinstance(block, dict) and block:
            return True
    return False


def _official_day(payload: dict[str, object]) -> date | None:
    """The date printed on ``official_datetime`` or ``kaisai_date``. None when absent."""
    data = payload.get("data")
    raw: object = None
    if isinstance(data, dict):
        raw = data.get("official_datetime") or data.get("kaisai_date")
    if raw is None:
        raw = payload.get("official_datetime")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    match = _DATED.search(text)
    if match is None and _DATED_COMPACT.fullmatch(text):
        match = _DATED_COMPACT.fullmatch(text)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
