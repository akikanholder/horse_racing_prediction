"""Final combination odds for one past race.

Wide, umaren, and sanrenpuku. A price is final only when the settled result
is confirmed and complete. The page chain is static HTML, JSON already in
that page, a same-site odds file the page itself names, then the same
netkeiba odds API the live ``odds`` command calls. Headless runs only when
that API did not return a final market and the HTML is not a live board.
A 403, a login wall, or a challenge stops that chain. Prices are not invented
from payouts, and a live board is not copied.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from backend.models.odds import ODDS_ENCODING, BetType, FinalRaceOdds, NonRunner, OddsQuote
from backend.models.race import SettledRaceResult
from backend.scrapers.final_odds import (
    COMBO_ODDS_MISSING,
    FINAL_ODDS_NOT_ON_PAGE,
    FINAL_ODDS_NOT_SETTLED,
    PublicOddsPages,
    final_odds_need_fallback,
    label_final_market_quote,
    parse_confirmed_odds,
    payout_disagreements,
    priced_without_final_label,
    quotes_from_result_api,
    same_site_odds_urls,
)
from backend.scrapers.jra_headless import FetchMode, close_shared
from backend.scrapers.jra_scraper import JRAScraper
from backend.scrapers.netkeiba_odds_scraper import NetkeibaOddsScraper
from backend.scrapers.netkeiba_scraper import (
    RACE_RESULT_FORBIDDEN,
    RACE_RESULT_LOGIN,
    NetkeibaScraper,
)
from backend.scrapers.page_cache import CachingHTTPClient, default_page_cache_dir
from backend.scrapers.page_fallback import (
    HEADLESS_UNAVAILABLE,
    AccessBlocked,
    FetchedBody,
    HeadlessUnavailableError,
    PageFallback,
    render_headless,
)
from backend.scrapers.runner_status import (
    NETKEIBA_ODDS_FORBIDDEN,
    NETKEIBA_ODDS_LOGIN,
    join_warnings,
)
from backend.utils.date_utils import tokyo_now

logger = logging.getLogger(__name__)

_COMBO_TYPES: tuple[BetType, ...] = ("wide", "umaren", "sanrenpuku")
_PAGE_TYPE = {"wide": "b5", "umaren": "b4", "sanrenpuku": "b7"}
_ODDS_PAGE = "https://race.netkeiba.com/odds/index.html"
HeadlessChoice = Callable[[str], str] | Literal["auto", "off"]


class FinalOddsService:
    """Read final wide, umaren, and sanrenpuku odds for one race id."""

    def __init__(
        self,
        *,
        netkeiba: NetkeibaScraper | None = None,
        jra: JRAScraper | None = None,
        http: CachingHTTPClient | None = None,
        headless_render: HeadlessChoice = "off",
        fetch_mode: FetchMode = "html",
    ):
        self._owns_clients = netkeiba is None and jra is None
        self.headless_render = headless_render
        self.fetch_mode = fetch_mode
        self.http = http
        if self._owns_clients:
            self.http = CachingHTTPClient(default_page_cache_dir(), min_request_interval=1.5)
            self.netkeiba = NetkeibaScraper(self.http, headless_render=headless_render)
            self.netkeiba.fetch_mode = fetch_mode
            self.jra = JRAScraper(
                self.http,
                headless_render=headless_render,
                fetch_mode=fetch_mode,
            )
        else:
            if netkeiba is None or jra is None:
                raise ValueError("netkeiba and jra are passed together")
            self.netkeiba = netkeiba
            self.jra = jra

    def close(self) -> None:
        """Close clients this service constructed."""
        if not self._owns_clients:
            return
        self.netkeiba.close()
        self.jra.close()
        if self.http is not None:
            self.http.close()
        close_shared()

    def __enter__(self) -> FinalOddsService:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> Literal[False]:
        self.close()
        return False

    def get(
        self,
        race_id: str,
        bet_types: str | list[str] | None = None,
    ) -> FinalRaceOdds:
        """Final combination odds. Null prices when the race is not final."""
        fetched_at = tokyo_now()
        selected = _combo_bet_types(bet_types)
        if not selected:
            return _empty(
                race_id,
                [],
                fetched_at,
                reason="combination odds were not requested",
                warning="combination odds were not requested",
            )
        settled = self.netkeiba.get_settled_race_result(race_id, fetched_at=fetched_at)
        if _result_blocked(settled):
            return _empty(
                race_id,
                selected,
                fetched_at,
                reason=settled.warning,
                warning=settled.warning,
                result_phase=settled.result_phase,
                complete=settled.complete,
            )
        if settled.result_phase != "confirmed" or not settled.complete:
            warning = _not_final_warning(settled)
            return _empty(
                race_id,
                selected,
                fetched_at,
                reason=warning,
                warning=warning,
                result_phase=settled.result_phase,
                complete=settled.complete,
            )
        return self._confirmed(race_id, selected, settled, fetched_at)

    def _confirmed(
        self,
        race_id: str,
        selected: list[BetType],
        settled: SettledRaceResult,
        fetched_at: datetime,
    ) -> FinalRaceOdds:
        client = self._http_client()
        previous = False
        if client is not None and not _past_meeting(settled):
            previous = bool(getattr(client, "replay_disabled", False))
            client.replay_disabled = True  # type: ignore[attr-defined]
        try:
            quotes, runners, warning = self._load(race_id, selected, fetched_at, settled)
        finally:
            if client is not None and not _past_meeting(settled):
                client.replay_disabled = previous  # type: ignore[attr-defined]
        if not quotes:
            note = warning or FINAL_ODDS_NOT_ON_PAGE
            return _empty(
                race_id,
                selected,
                fetched_at,
                reason=note,
                warning=note,
                result_phase="confirmed",
                complete=True,
            )
        labeled = [label_final_market_quote(quote) for quote in quotes]
        return FinalRaceOdds(
            race_id=race_id,
            bet_types=selected,
            prefer="live",
            status="result",
            reason=None,
            encoding=ODDS_ENCODING,
            race_phase="post",
            result_phase="confirmed",
            complete=True,
            odds_final=True,
            quotes=labeled,
            non_runners=runners,
            warning=warning,
            fetched_at=fetched_at,
        )

    def _load(
        self,
        race_id: str,
        selected: list[BetType],
        fetched_at: datetime,
        settled: SettledRaceResult,
    ) -> tuple[list[OddsQuote], list[NonRunner] | None, str | None]:
        notes: list[str | None] = []
        pages = self.jra.fetch_odds_pages(race_id, fetched_at=fetched_at)
        # A refused JRA page is not parsed. netkeiba is a different site, tried once.
        jra_pages = [] if pages.blocked else list(pages.pages)
        notes.append(pages.warning)
        quotes, runners = _quotes_from_pages(jra_pages, race_id, selected)
        missing = _missing(selected, quotes)
        stopped = False
        if missing:
            extra, extra_runners, extra_note, stopped = self._netkeiba_chain(
                race_id, missing, fetched_at
            )
            notes.append(extra_note)
            quotes = _fill(quotes, extra)
            runners = _merge_runners(runners, extra_runners)
        # A 403 already explains the gap. Do not also say the page omitted the market.
        still = [] if stopped else _missing(selected, quotes)
        if still:
            notes.append(f"{COMBO_ODDS_MISSING}: {', '.join(still)}")
        elif not quotes and not stopped:
            notes.append(FINAL_ODDS_NOT_ON_PAGE)
        notes.extend(payout_disagreements(quotes, settled.payouts))
        ordered = sorted(
            quotes,
            key=lambda quote: (selected.index(quote.bet_type), quote.combination),
        )
        return ordered, runners, join_warnings(*notes)

    def _netkeiba_chain(
        self,
        race_id: str,
        bet_types: list[BetType],
        fetched_at: datetime,
    ) -> tuple[list[OddsQuote], list[NonRunner], str | None, bool]:
        """HTML, then the odds API, then headless. A refusal stops the rest.

        The API is the one ``odds`` calls. Only ``race_phase`` ``post`` is
        kept. A live board that already prints numbers is not sent to headless.
        """
        client = self._http_client()
        if client is None:
            return [], [], None, False
        scraper = NetkeibaOddsScraper(http_client=client)  # type: ignore[arg-type]
        quotes: list[OddsQuote] = []
        runners: list[NonRunner] = []
        notes: list[str | None] = []
        stopped = False
        for bet_type in bet_types:
            loaded = _load_odds_page(client, race_id, bet_type, fetched_at, "off")
            if loaded.blocked:
                notes.append(_netkeiba_block_warning(loaded.warning))
                stopped = True
                break
            notes.append(loaded.warning)
            extra, extra_runners = _quotes_from_pages(loaded.pages, race_id, [bet_type])
            quotes = _fill(quotes, extra)
            runners = _merge_runners(runners, extra_runners) or []
            if _covers(quotes, bet_type):
                continue
            api_quotes, api_runners, api_note, api_stopped = _result_api_quotes(
                scraper, race_id, bet_type, fetched_at
            )
            if api_stopped:
                notes.append(api_note)
                stopped = True
                break
            quotes = _fill(quotes, api_quotes)
            runners = _merge_runners(runners, api_runners) or []
            if _covers(quotes, bet_type) or _printed_live_board(loaded.pages):
                continue
            rendered, blocked, missing_browser = _render_odds_page(
                race_id, bet_type, self.headless_render
            )
            if blocked is not None:
                notes.append(_netkeiba_block_warning(_block_text(blocked)))
                stopped = True
                break
            if missing_browser:
                notes.append(missing_browser)
            if rendered is None:
                continue
            page = FetchedBody(rendered, "headless", fetched_at, _odds_url(race_id, bet_type))
            head_quotes, head_runners = _quotes_from_pages([page], race_id, [bet_type])
            quotes = _fill(quotes, head_quotes)
            runners = _merge_runners(runners, head_runners) or []
        return quotes, runners, join_warnings(*notes), stopped

    def _http_client(self) -> object | None:
        if self.http is not None:
            return self.http
        return getattr(self.netkeiba, "http_client", None)


def _load_odds_page(
    client: object,
    race_id: str,
    bet_type: BetType,
    fetched_at: datetime,
    headless_render: HeadlessChoice,
) -> PublicOddsPages:
    fallback = PageFallback(
        client,
        site="netkeiba",
        headless_render=headless_render,
        url_finder=same_site_odds_urls,
    )
    loaded = fallback.load(
        url=_ODDS_PAGE,
        params={"type": _PAGE_TYPE[bet_type], "race_id": race_id},
        headers={"Referer": f"https://race.netkeiba.com/race/result.html?race_id={race_id}"},
        encoding="utf-8",
        fetched_at=fetched_at,
        should_fallback=lambda html: _page_needs_fallback(html, race_id, bet_type, fetched_at),
        still_incomplete=lambda pages: not _pages_cover(pages, race_id, bet_type),
    )
    if loaded.status == "blocked":
        return PublicOddsPages(loaded.pages, loaded.warning, True)
    warning = HEADLESS_UNAVAILABLE if loaded.warning == HEADLESS_UNAVAILABLE else loaded.warning
    return PublicOddsPages(loaded.pages, warning, False)


def _page_needs_fallback(html: str, race_id: str, bet_type: BetType, fetched_at: datetime) -> bool:
    """Keep walking only while this document has not shown a final price.

    A live board that already prints numbers is finished: those numbers are
    not final, and a later step must not be asked to invent them.
    """
    if not final_odds_need_fallback(html):
        return False
    page = FetchedBody(html, "netkeiba_html", fetched_at, _ODDS_PAGE)
    return not _pages_cover([page], race_id, bet_type)


def _pages_cover(pages: list[FetchedBody], race_id: str, bet_type: BetType) -> bool:
    quotes, _runners = _quotes_from_pages(pages, race_id, [bet_type])
    return any(_priced(quote) for quote in quotes if quote.bet_type == bet_type)


def _quotes_from_pages(
    pages: list[FetchedBody],
    race_id: str,
    selected: list[BetType],
) -> tuple[list[OddsQuote], list[NonRunner]]:
    parsed = parse_confirmed_odds(pages, race_id)
    quotes = [quote for quote in parsed.quotes if quote.bet_type in selected]
    seen = {(quote.bet_type, quote.combination) for quote in quotes}
    for page in pages:
        for bet_type in selected:
            for quote in quotes_from_result_api(page, race_id, bet_type):
                key = (quote.bet_type, quote.combination)
                if key in seen:
                    continue
                seen.add(key)
                quotes.append(quote)
    return quotes, list(parsed.non_runners)


def _covers(quotes: list[OddsQuote], bet_type: BetType) -> bool:
    return any(_priced(quote) for quote in quotes if quote.bet_type == bet_type)


def _priced(quote: OddsQuote) -> bool:
    if quote.withheld:
        return True
    if quote.bet_type == "wide":
        return quote.odds_min is not None and quote.odds_max is not None
    return quote.odds is not None


def _printed_live_board(pages: list[FetchedBody]) -> bool:
    """True when the HTML already printed numbers that are not final."""
    return any(priced_without_final_label(page.body) for page in pages)


def _odds_url(race_id: str, bet_type: BetType) -> str:
    return f"{_ODDS_PAGE}?type={_PAGE_TYPE[bet_type]}&race_id={race_id}"


def _result_api_quotes(
    scraper: NetkeibaOddsScraper,
    race_id: str,
    bet_type: BetType,
    fetched_at: datetime,
) -> tuple[list[OddsQuote], list[NonRunner], str | None, bool]:
    """The odds-command API. Only a non-empty ``result`` market is kept.

    ``middle`` and ``yoso`` are discarded and do not become a warning.
    HTTP 401 and 403 stop the chain.
    """
    fetch = {
        "wide": scraper.get_wide_odds,
        "umaren": scraper.get_umaren_odds,
        "sanrenpuku": scraper.get_sanrenpuku_odds,
    }[bet_type]
    snapshot = fetch(race_id, fetched_at=fetched_at)
    reason = snapshot.reason or ""
    if reason.startswith("http 403"):
        return [], [], NETKEIBA_ODDS_FORBIDDEN, True
    if reason.startswith("http 401"):
        return [], [], NETKEIBA_ODDS_LOGIN, True
    if snapshot.race_phase != "post":
        return [], [], None, False
    quotes = [quote for quote in snapshot.quotes if quote.bet_type == bet_type and _priced(quote)]
    return quotes, list(snapshot.non_runners or []), None, False


def _render_odds_page(
    race_id: str,
    bet_type: BetType,
    headless_render: HeadlessChoice,
) -> tuple[str | None, AccessBlocked | None, str | None]:
    """Headless only. A missing browser is a warning, not an access denial.

    ``off`` stays silent. ``auto`` and an explicit renderer attach
    ``headless browser is not installed`` when the browser is absent.
    """
    if headless_render == "off":
        return None, None, None
    render = render_headless if headless_render == "auto" else headless_render
    try:
        body = render(_odds_url(race_id, bet_type))
    except AccessBlocked as blocked:
        return None, blocked, None
    except HeadlessUnavailableError:
        return None, None, HEADLESS_UNAVAILABLE
    except Exception as exc:
        logger.warning("Headless odds render failed for %s %s: %s", race_id, bet_type, exc)
        return None, None, None
    if not isinstance(body, str):
        return None, None, None
    return body, None, None


def _block_text(blocked: AccessBlocked) -> str:
    if blocked.status == 403:
        return "netkeiba returned 403"
    return "netkeiba requires login"


def _missing(selected: list[BetType], quotes: list[OddsQuote]) -> list[BetType]:
    present = {quote.bet_type for quote in quotes if _priced(quote)}
    return [bet_type for bet_type in selected if bet_type not in present]


def _fill(primary: list[OddsQuote], extra: list[OddsQuote]) -> list[OddsQuote]:
    seen = {(quote.bet_type, quote.combination) for quote in primary}
    merged = list(primary)
    for quote in extra:
        key = (quote.bet_type, quote.combination)
        if key in seen:
            continue
        seen.add(key)
        merged.append(quote)
    return merged


def _merge_runners(
    primary: list[NonRunner] | None,
    extra: list[NonRunner],
) -> list[NonRunner] | None:
    if primary is None and not extra:
        return None
    found: list[NonRunner] = list(primary or [])
    known = {runner.horse_number for runner in found}
    for runner in extra:
        if runner.horse_number not in known:
            found.append(runner)
            known.add(runner.horse_number)
    return found


def _past_meeting(settled: SettledRaceResult) -> bool:
    """True when the printed race day is before today in Asia/Tokyo."""
    if settled.race_date is None:
        return False
    return settled.race_date < tokyo_now().date()


def _result_blocked(settled: SettledRaceResult) -> bool:
    if settled.http_status in {401, 403} or settled.login_required:
        return True
    warning = settled.warning or ""
    return RACE_RESULT_FORBIDDEN in warning or RACE_RESULT_LOGIN in warning


def _not_final_warning(settled: SettledRaceResult) -> str:
    """Keep the result page's own reason. Do not rewrite a miss as incomplete."""
    if settled.result_phase == "provisional":
        return "result phase is provisional"
    if settled.warning:
        return settled.warning
    if not settled.complete:
        return "race result is not complete"
    if settled.result_phase is None:
        return "result phase was not on the page"
    return FINAL_ODDS_NOT_SETTLED


def _netkeiba_block_warning(warning: str | None) -> str:
    if warning and "403" in warning:
        return NETKEIBA_ODDS_FORBIDDEN
    if warning and "login" in warning:
        return NETKEIBA_ODDS_LOGIN
    return warning or NETKEIBA_ODDS_FORBIDDEN


def _combo_bet_types(bet_types: str | list[str] | None) -> list[BetType]:
    if bet_types is None:
        return list(_COMBO_TYPES)
    raw: list[BetType] = []
    items = [bet_types] if isinstance(bet_types, str) else list(bet_types)
    for item in items:
        for part in str(item).split(","):
            name = part.strip().lower()
            if name != "wide" and name != "umaren" and name != "sanrenpuku":
                continue
            if name not in raw:
                raw.append(name)
    return raw


def _empty(
    race_id: str,
    bet_types: list[BetType],
    fetched_at: datetime,
    *,
    reason: str | None,
    warning: str | None,
    result_phase: Literal["provisional", "confirmed"] | None = None,
    complete: bool | None = None,
) -> FinalRaceOdds:
    return FinalRaceOdds(
        race_id=race_id,
        bet_types=bet_types,
        prefer="live",
        status="unavailable",
        reason=reason,
        encoding=ODDS_ENCODING,
        race_phase=None,
        result_phase=result_phase,
        complete=complete,
        odds_final=False,
        quotes=[],
        non_runners=None,
        warning=warning,
        fetched_at=fetched_at,
    )
