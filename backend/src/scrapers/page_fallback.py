"""Public-page fetch with a reuseable fallback chain.

Order is plain HTTP, then JSON embedded in that page or a same-site JSON URL
the page itself calls, then an optional headless render. HTTP 401/403 and a
login wall stop the chain. This module does not change the User-Agent, solve a
captcha, or send credentials. Callers that need a logged-in page pass their own
session, built from environment credentials, on the HTTP client.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADLESS_UNAVAILABLE = "headless browser is not installed"
PAGE_FORBIDDEN = "returned 403"
PAGE_LOGIN = "requires login"
PAGE_CAPTCHA = "returned a captcha"
PAGE_CHALLENGE = "returned a challenge"

BlockedReason = Literal["captcha", "challenge", "login", "http_403"]

_JSON_URL = re.compile(
    r"""(?:url|URL)\s*:\s*['"]([^'"]+)['"]""",
)
_RESULT_API = re.compile(r"/api/[^\"'\s]*result", re.IGNORECASE)
_HORSE_RESULT_PATH = re.compile(
    r"/horse/[^\"'\s]*result|/api/[^\"'\s]*result",
    re.IGNORECASE,
)
_BLOCKED_URL = re.compile(r"login|captcha|signin|odds", re.IGNORECASE)


class _StatusResponse:
    """Enough of an HTTP response for callers that read ``status_code``."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class AccessBlocked(Exception):
    """The site refused the page. Do not try another identity.

    ``status`` is an HTTP status only when the response itself was 401 or 403.
    A captcha or challenge page leaves it null and sets ``reason``.
    """

    def __init__(
        self,
        status: int | None,
        message: str,
        *,
        reason: BlockedReason | None = None,
    ):
        super().__init__(message)
        if reason is None:
            if status == 401:
                reason = "login"
            elif status == 403:
                reason = "http_403"
        self.reason = reason
        self.status = status if isinstance(status, int) else None
        self.response = _StatusResponse(self.status) if self.status is not None else None


class HeadlessUnavailableError(Exception):
    """Playwright or its browser is not installed."""


@dataclass
class FetchedBody:
    """One document produced by a single method."""

    body: str
    source: str
    fetched_at: datetime
    url: str


@dataclass
class FallbackFetch:
    """Bodies collected before the chain stopped.

    ``http_status`` is set only for a real HTTP 401 or 403. A captcha page,
    a challenge page, and a payout of 403円 do not set it. ``blocked_reason``
    names the stop: ``captcha``, ``challenge``, ``login``, or ``http_403``.
    ``login_required`` is true only for a login wall.
    """

    status: Literal["ok", "blocked", "failed"]
    pages: list[FetchedBody] = field(default_factory=list)
    warning: str | None = None
    http_status: int | None = None
    login_required: bool = False
    blocked_reason: BlockedReason | None = None


def looks_like_login_wall(html: str) -> bool:
    """True when the document is a login form, not a page that merely links to one."""
    soup = BeautifulSoup(html, "lxml")
    if soup.find("input", attrs={"type": "password"}) is None:
        return False
    title = soup.title.get_text(" ", strip=True) if soup.title is not None else ""
    heading = ""
    h1 = soup.find("h1")
    if h1 is not None:
        heading = h1.get_text(" ", strip=True)
    label = f"{title} {heading}".lower()
    return "ログイン" in label or "login" in label


# Status phrasing, not a digit sequence. 「第403回」 and 「403万円」 are not denials.
# Cloudflare's interstitial title is exactly 「Just a moment...」. A title that
# only contains those words is not a challenge. Japanese captcha titles such
# as 「CAPTCHA認証」 have no word boundary after the Latin token, so captcha
# is not limited to ``\b``.
_CAPTCHA_LABEL = re.compile(r"captcha|キャプチャ|画像認証", re.IGNORECASE)
_FORBIDDEN_LABEL = re.compile(r"\baccess\s+denied\b|\b403\s*forbidden\b", re.IGNORECASE)
_SHORT_FORBIDDEN = re.compile(r"^(?:403|forbidden|403\s*forbidden)$", re.IGNORECASE)
_CLOUDFLARE_TITLE = re.compile(r"just a moment(?:\.\.\.|…)", re.IGNORECASE)
# A challenge form, id, or class. ``cf-challenge`` is a prefix, so
# ``cf-challenge-running`` counts. ``class="cf-browser-verification"`` counts.
# Cloudflare also injects ``/cdn-cgi/challenge-platform/scripts/jsd/main.js``
# into ordinary pages. That script tag is not a challenge page.
_CHALLENGE_FORM = re.compile(
    r"""\b(?:id|name|class)\s*=\s*['"][^'"]*\b"""
    r"""(?:cf-challenge[\w-]*|challenge-form|cf-browser-verification)(?![\w-])""",
    re.IGNORECASE,
)
# ``_`` is a word character, so ``\bcf_chl_opt\b`` misses ``window._cf_chl_opt``
# and ``__cf_chl_opt``. A letter or digit glued on the left still does not match.
_CF_CHL_OPT = re.compile(r"(?<![A-Za-z0-9])_*cf_chl_opt\b", re.IGNORECASE)
_CLOUDFLARE_ATTENTION = re.compile(
    r"attention required!\s*\|\s*cloudflare",
    re.IGNORECASE,
)


def _compact_label(label: str) -> str:
    return " ".join(label.split())


def _captcha_label(label: str) -> bool:
    compact = _compact_label(label)
    return bool(compact) and _CAPTCHA_LABEL.search(compact) is not None


def _forbidden_label(label: str) -> bool:
    compact = _compact_label(label)
    if not compact:
        return False
    if _FORBIDDEN_LABEL.search(compact):
        return True
    return _SHORT_FORBIDDEN.fullmatch(compact) is not None


def _challenge_title(label: str) -> bool:
    compact = _compact_label(label)
    if not compact:
        return False
    if _CLOUDFLARE_TITLE.fullmatch(compact) is not None:
        return True
    return _CLOUDFLARE_ATTENTION.fullmatch(compact) is not None


def _lacks_real_content(soup: BeautifulSoup) -> bool:
    """True when the document is an interstitial, not a race or article page.

    ``cf_chl_opt`` on a page that already has a table or a long body is not
    treated as the challenge. The challenge form ids are.
    """
    for script in soup.find_all("script"):
        script.decompose()
    text = soup.get_text(" ", strip=True)
    if soup.find("table") and len(text) > 40:
        return False
    return len(text) <= 180


def _page_labels(html: str) -> tuple[BeautifulSoup, str, str, str]:
    soup = BeautifulSoup(html or "", "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title is not None else ""
    heading = ""
    h1 = soup.find("h1")
    if h1 is not None:
        heading = h1.get_text(" ", strip=True)
    text = soup.get_text(" ", strip=True)
    return soup, title, heading, text


def page_block_reason(html: str) -> BlockedReason | None:
    """Why this document is a refusal, or None when it is a normal page.

    A captcha title is ``captcha``. A challenge form, id, or class
    (``cf-challenge`` as a prefix, including ``cf-challenge-running``,
    ``challenge-form``, ``cf-browser-verification``), the exact title
    「Just a moment...」, or the exact title 「Attention Required! | Cloudflare」
    is ``challenge``. ``window._cf_chl_opt`` and ``__cf_chl_opt`` are a
    challenge only when the page has no real content. The script
    ``/cdn-cgi/challenge-platform/scripts/jsd/main.js`` is not a challenge.
    An access-denied title is ``http_403`` without claiming that an HTTP
    status was observed.
    """
    raw = html or ""
    soup, title, heading, text = _page_labels(raw)
    if _challenge_title(title) or _challenge_title(heading):
        return "challenge"
    if _CHALLENGE_FORM.search(raw):
        return "challenge"
    if _CF_CHL_OPT.search(raw) and _lacks_real_content(soup):
        return "challenge"
    if _captcha_label(title) or _captcha_label(heading):
        return "captcha"
    if _forbidden_label(title) or _forbidden_label(heading):
        return "http_403"
    if len(text) <= 80:
        if _captcha_label(text):
            return "captcha"
        if _forbidden_label(text):
            return "http_403"
        if _challenge_title(text):
            return "challenge"
    return None


def looks_like_forbidden_page(html: str) -> bool:
    """True when the document is a 403, access-denied, captcha, or challenge page.

    A payout of 403円, a title 「第403回」, or 「403万円」 is not a refusal.
    The title and the first heading name the page. Cloudflare is a challenge
    when the title is exactly 「Just a moment...」 or exactly
    「Attention Required! | Cloudflare」, the document has a challenge id,
    name, or class (``cf-challenge`` as a prefix, ``challenge-form``,
    ``cf-browser-verification``), or ``window._cf_chl_opt`` appears on a page
    with no real content. A normal page that only includes
    ``/cdn-cgi/challenge-platform/scripts/jsd/main.js`` is not a challenge.
    A title that merely contains "just a moment" is not a challenge.
    「CAPTCHA認証」 and 「キャプチャ」 are captcha titles: callers stop and do
    not try another identity.
    """
    return page_block_reason(html) in {"captcha", "challenge", "http_403"}


def _blocked_for_page(html: str) -> AccessBlocked | None:
    """An HTML refusal. HTTP status stays null unless the caller saw one."""
    reason = page_block_reason(html)
    if reason == "captcha":
        return AccessBlocked(None, PAGE_CAPTCHA, reason="captcha")
    if reason == "challenge":
        return AccessBlocked(None, PAGE_CHALLENGE, reason="challenge")
    if reason == "http_403":
        # The title says forbidden. That is not an HTTP status.
        return AccessBlocked(None, PAGE_FORBIDDEN, reason="http_403")
    return None


def _open_shared_browser() -> object | None:
    """The JRA Chromium when one is already open. Does not start Playwright."""
    try:
        from backend.scrapers.jra_headless import running_shared_session
    except Exception:
        return None
    return running_shared_session()


def headless_browser_installed() -> bool:
    """True when Chromium is already open, or Playwright can launch one.

    A JRA fetch may already hold the process-wide browser. Starting a second
    Playwright driver then fails, which is not the same as a missing install.
    """
    if _open_shared_browser() is not None:
        return True
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as playwright:
            executable = playwright.chromium.executable_path
    except Exception:
        return _open_shared_browser() is not None
    if not executable:
        return False
    from pathlib import Path

    return Path(executable).is_file()


# The post-race flash paints five ``FirstDisplay`` rows before the rest of the
# table. ``domcontentloaded`` is that flash. Wait until the row count covers
# the printed field, or the wait times out and the parser marks the result
# incomplete. A 401/403 is raised before this runs and is not retried.
RESULT_TABLE_WAIT_MS = 15_000
RESULT_TABLE_READY_JS = """() => {
  const table = document.querySelector('#All_Result_Table, table[summary="全着順"]');
  if (!table) return false;
  const rows = Array.from(table.querySelectorAll('tr')).filter((tr) => tr.querySelector('td'));
  if (!rows.length) return false;
  const className = String(table.className || '');
  const flash = !className.includes('Table_Show_All')
    && rows.every((tr) => String(tr.className || '').includes('FirstDisplay'));
  const header = document.querySelector('.RaceData02');
  const text = header ? (header.textContent || '') : '';
  const match = text.match(/(\\d+)\\s*頭/);
  if (!match) return !flash;
  const starters = Number(match[1]);
  // rows.length - nonRunners < starters - nonRunners is rows.length < starters.
  if (rows.length < starters) return false;
  if (flash) return false;
  return true;
}"""


# Odds pages paint ``---.-`` and fill the cells from the page's own script.
# One filled cell, or a final heading while other cells are still ``---.-``,
# is not ready. ``----`` is a printed withheld mark. A timeout is the shell.
# The caller parses it and does not invent a price.
ODDS_CELL_WAIT_MS = 12_000
ODDS_CELL_READY_JS = """() => {
  const compact = (el) => (el.textContent || '').replace(/\\s+/g, '');
  const loading = new Set(['---.-', '']);
  const nodes = Array.from(document.querySelectorAll(
    'span[id^="odds-"], span[id^="oddsmin-"], span[id^="oddsmax-"], td.Odds, span.Odds'
  ));
  if (!nodes.length) return false;
  return nodes.every((el) => !loading.has(compact(el)));
}"""


# The baba page fills ``#cushion_list`` / ``#moist_list`` from its own script
# after DOMContentLoaded. A timeout is that shell. The caller parses it and
# does not fetch again.
BABA_READING_WAIT_MS = 12_000
BABA_READING_READY_JS = """() => {
  const nodes = ['#cushion_list', '#moist_list']
    .map((sel) => document.querySelector(sel))
    .filter(Boolean);
  if (!nodes.length) return true;
  return nodes.every((el) => el.options && el.options.length > 0);
}"""


def await_page_ready(page: object, url: str) -> None:
    """Wait until the document has the reading the page itself fills in.

    A result URL waits for the full finishing table. An odds URL waits until
    every visible odds cell is filled. One number, or a final heading while
    other cells are still ``---.-`` or empty, does not end the wait. ``----``
    is a withheld mark and counts as filled. No odds cells means keep waiting.
    A baba URL waits for the measurement dropdown. Anything else returns
    immediately. A timeout is the page still short. The caller parses that
    document and does not fetch again.
    """
    if "/race/result.html" in url:
        script, timeout, note = (
            RESULT_TABLE_READY_JS,
            RESULT_TABLE_WAIT_MS,
            "Result table was still short",
        )
    elif "/odds/" in url:
        script, timeout, note = (
            ODDS_CELL_READY_JS,
            ODDS_CELL_WAIT_MS,
            "Odds cells were still placeholders",
        )
    elif "/keiba/baba/" in url:
        script, timeout, note = (
            BABA_READING_READY_JS,
            BABA_READING_WAIT_MS,
            "Baba measurement dropdown was still empty",
        )
    else:
        return
    waiter = getattr(page, "wait_for_function", None)
    if not callable(waiter):
        return
    try:
        waiter(script, timeout=timeout)
    except Exception as exc:
        logger.info("%s for %s: %s", note, url, exc)


def render_headless(url: str) -> str:
    """Render ``url`` in a headless browser.

    Uses the browser's own user agent. A 401/403 response is not retried.
    A race result page waits for the full finishing table. A baba page waits
    for the measurement dropdown the page itself fills. A live track page
    sends ``Cache-Control: no-cache``. When a JRA fetch already opened the
    shared Chromium, that session renders the page. A second driver is not
    started.
    """
    session = _open_shared_browser()
    if session is not None:
        getter = getattr(session, "get", None)
        if not callable(getter):
            raise HeadlessUnavailableError(HEADLESS_UNAVAILABLE)
        body = getter(url)
        if not isinstance(body, str):
            raise HeadlessUnavailableError(HEADLESS_UNAVAILABLE)
        return body
    if not headless_browser_installed():
        raise HeadlessUnavailableError(HEADLESS_UNAVAILABLE)
    from playwright.sync_api import sync_playwright

    from backend.scrapers.track_freshness import fresh_track_headers

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            fresh = fresh_track_headers(url)
            if fresh:
                page.set_extra_http_headers(fresh)
            response = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            status = response.status if response is not None else None
            if status in {401, 403}:
                raise AccessBlocked(
                    status,
                    PAGE_FORBIDDEN if status == 403 else PAGE_LOGIN,
                    reason="http_403" if status == 403 else "login",
                )
            await_page_ready(page, url)
            html = str(page.content())
        finally:
            browser.close()
    if looks_like_login_wall(html):
        raise AccessBlocked(401, PAGE_LOGIN, reason="login")
    blocked = _blocked_for_page(html)
    if blocked is not None:
        raise blocked
    return html


def embedded_json_payloads(html: str) -> list[str]:
    """JSON the page already inlined. Unrelated scripts are left for the caller to ignore."""
    soup = BeautifulSoup(html, "lxml")
    payloads: list[str] = []
    for script in soup.find_all("script"):
        kind = str(script.get("type") or "").lower()
        text = script.string if script.string is not None else script.get_text()
        if not text or not text.strip():
            continue
        if "json" in kind:
            payloads.append(text.strip())
    return payloads


def same_site_result_urls(html: str, page_url: str) -> list[str]:
    """Absolute URLs of same-site result JSON the page's own scripts call.

    Advertisements, odds forms, and login endpoints are not included.
    """
    found: list[str] = []
    seen: set[str] = set()
    page = urlparse(page_url)
    for match in _JSON_URL.finditer(html):
        raw = match.group(1).strip()
        if not _RESULT_API.search(raw.split("?", 1)[0]):
            continue
        absolute = urljoin(page_url, raw)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.netloc != page.netloc:
            continue
        if absolute in seen or absolute.split("?", 1)[0] == page_url.split("?", 1)[0]:
            continue
        seen.add(absolute)
        found.append(absolute)
    return found


def same_site_horse_result_urls(html: str, page_url: str) -> list[str]:
    """Absolute URLs of same-site horse-result documents the page's own scripts call.

    Login, captcha, odds, and other hosts are not included. The result page
    itself is not treated as a second request.
    """
    found: list[str] = []
    seen: set[str] = set()
    page = urlparse(page_url)
    for match in _JSON_URL.finditer(html):
        raw = match.group(1).strip()
        if _BLOCKED_URL.search(raw):
            continue
        path = raw.split("?", 1)[0]
        if not _HORSE_RESULT_PATH.search(path):
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


def _status_code(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


class PageFallback:
    """Fetch one public page, then fall back only when ``should_fallback`` says so.

    ``headless_render`` is ``"auto"`` (Playwright when installed), ``"off"``, or a
    callable used by tests. ``"off"`` does not add a warning. A missing browser
    on the auto path sets ``warning`` when a later step was still needed.
    """

    def __init__(
        self,
        http_client: object,
        *,
        site: str = "netkeiba",
        headless_render: Callable[[str], str] | Literal["auto", "off"] = "auto",
        url_finder: Callable[[str, str], list[str]] | None = None,
    ):
        self.http_client = http_client
        self.site = site
        self._headless_render = headless_render
        self._url_finder = url_finder or same_site_result_urls

    def load(
        self,
        *,
        url: str,
        params: dict[str, str] | None,
        headers: dict[str, str] | None,
        encoding: str | None,
        fetched_at: datetime,
        should_fallback: Callable[[str], bool],
        still_incomplete: Callable[[list[FetchedBody]], bool] | None = None,
        html_body: str | None = None,
        document_source: str | None = None,
    ) -> FallbackFetch:
        """GET ``url``. On 403 or a login wall, return no further documents.

        ``html_body`` skips that GET when the caller already has the document.
        ``should_fallback`` looks at the first HTML body. When it is true, JSON
        already in the page and same-site URLs the page calls are read next.
        Headless runs only when ``still_incomplete`` is still true.
        """
        if html_body is None:
            try:
                html = self._get(url, params=params, headers=headers, encoding=encoding)
            except AccessBlocked as blocked:
                return blocked_fetch(self.site, blocked)
            except Exception as exc:
                logger.error("Page fetch failed for %s: %s", url, exc)
                return FallbackFetch(status="failed", warning=None)
        else:
            html = html_body

        if looks_like_login_wall(html):
            return blocked_fetch(self.site, AccessBlocked(401, PAGE_LOGIN, reason="login"))
        blocked = _blocked_for_page(html)
        if blocked is not None:
            return blocked_fetch(self.site, blocked)

        page_url = _with_query(url, params)
        first_source = document_source or f"{self.site}_html"
        pages = [
            FetchedBody(
                body=html,
                source=first_source,
                fetched_at=fetched_at,
                url=page_url,
            )
        ]
        if not should_fallback(html):
            return FallbackFetch(status="ok", pages=pages)

        json_source = "headless" if document_source == "headless" else f"{self.site}_js"
        for payload in embedded_json_payloads(html):
            pages.append(
                FetchedBody(
                    body=payload,
                    source=json_source,
                    fetched_at=fetched_at,
                    url=page_url,
                )
            )
        try:
            endpoints = list(self._url_finder(html, page_url))
        except AccessBlocked as blocked:
            return blocked_fetch(self.site, blocked, pages)
        for endpoint in endpoints:
            try:
                body = self._get(endpoint, params=None, headers=headers, encoding=encoding)
            except AccessBlocked as blocked:
                return blocked_fetch(self.site, blocked, pages)
            except Exception as exc:
                logger.warning("Result endpoint %s failed: %s", endpoint, exc)
                continue
            if looks_like_login_wall(body):
                return blocked_fetch(
                    self.site, AccessBlocked(401, PAGE_LOGIN, reason="login"), pages
                )
            blocked = _blocked_for_page(body)
            if blocked is not None:
                return blocked_fetch(self.site, blocked, pages)
            fragment_source = f"{self.site}_js"
            if document_source == "headless" and getattr(self.http_client, "used_headless", False):
                fragment_source = "headless"
            pages.append(
                FetchedBody(
                    body=body,
                    source=fragment_source,
                    fetched_at=fetched_at,
                    url=endpoint,
                )
            )

        if still_incomplete is not None and not still_incomplete(pages):
            return FallbackFetch(status="ok", pages=pages)

        headless_warning: str | None = None
        rendered = self._render(page_url)
        if isinstance(rendered, str):
            pages.append(
                FetchedBody(
                    body=rendered,
                    source="headless",
                    fetched_at=fetched_at,
                    url=page_url,
                )
            )
        elif isinstance(rendered, AccessBlocked):
            return blocked_fetch(self.site, rendered, pages)
        elif isinstance(rendered, HeadlessUnavailableError):
            headless_warning = HEADLESS_UNAVAILABLE
        return FallbackFetch(status="ok", pages=pages, warning=headless_warning)

    def _get(
        self,
        url: str,
        *,
        params: dict[str, str] | None,
        headers: dict[str, str] | None,
        encoding: str | None,
    ) -> str:
        getter = getattr(self.http_client, "get", None)
        if not callable(getter):
            raise RuntimeError("http client has no get")
        try:
            body = getter(url, params=params, headers=headers, encoding=encoding)
        except Exception as exc:
            status = _status_code(exc)
            if status in {401, 403}:
                message = PAGE_FORBIDDEN if status == 403 else PAGE_LOGIN
                reason: BlockedReason = "http_403" if status == 403 else "login"
                raise AccessBlocked(status, message, reason=reason) from exc
            raise
        if not isinstance(body, str):
            raise TypeError("http client get did not return text")
        return body

    def _render(self, url: str) -> str | AccessBlocked | HeadlessUnavailableError | None:
        """Return HTML, a block, an unavailable error, or None when headless is off."""
        choice = self._headless_render
        if choice == "off":
            return None
        render = render_headless if choice == "auto" else choice
        try:
            return render(url)
        except AccessBlocked as blocked:
            return blocked
        except HeadlessUnavailableError as missing:
            return missing
        except Exception as exc:
            logger.warning("Headless render failed for %s: %s", url, exc)
            if not headless_browser_installed() and choice == "auto":
                return HeadlessUnavailableError(HEADLESS_UNAVAILABLE)
            return None


def blocked_fetch(
    site: str,
    blocked: AccessBlocked,
    pages: list[FetchedBody] | None = None,
) -> FallbackFetch:
    """Stop the chain. ``http_status`` is a real HTTP status, not the warning text."""
    reason = blocked.reason
    if reason is None:
        if blocked.status == 403:
            reason = "http_403"
        elif blocked.status == 401:
            reason = "login"
    http_status = blocked.status if blocked.status in {401, 403} else None
    if reason in {"captcha", "challenge"}:
        http_status = None
    return FallbackFetch(
        status="blocked",
        pages=list(pages or []),
        warning=_blocked_warning(site, reason),
        http_status=http_status,
        login_required=reason == "login",
        blocked_reason=reason,
    )


def _blocked_warning(site: str, reason: BlockedReason | None) -> str:
    if reason == "captcha":
        return f"{site} returned a captcha"
    if reason == "challenge":
        return f"{site} returned a challenge"
    if reason == "http_403":
        return f"{site} returned 403"
    return f"{site} requires login"


def _with_query(url: str, params: dict[str, str] | None) -> str:
    if not params:
        return url
    from urllib.parse import urlencode

    join = "&" if "?" in url else "?"
    return f"{url}{join}{urlencode(params)}"
