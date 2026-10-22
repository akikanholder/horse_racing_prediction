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
    """The site refused the page. Do not try another identity."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.response = _StatusResponse(status)


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
    """Bodies collected before the chain stopped."""

    status: Literal["ok", "blocked", "failed"]
    pages: list[FetchedBody] = field(default_factory=list)
    warning: str | None = None


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


def headless_browser_installed() -> bool:
    """True when Playwright can launch a Chromium that is already installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as playwright:
            executable = playwright.chromium.executable_path
    except Exception:
        return False
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
  let nonRunners = 0;
  for (const tr of rows) {
    const label = (tr.innerText || '').replace(/\\s/g, '');
    if (label.includes('取消') || label.includes('除外')) nonRunners += 1;
  }
  if (rows.length - nonRunners < starters - nonRunners) return false;
  if (flash) return false;
  return true;
}"""


def await_page_ready(page: object, url: str) -> None:
    """Wait for a full result table. Other URLs return immediately.

    A timeout is the page still showing the early rows. The caller parses
    that document and reports it incomplete. This does not fetch again.
    """
    if "/race/result.html" not in url:
        return
    waiter = getattr(page, "wait_for_function", None)
    if not callable(waiter):
        return
    try:
        waiter(RESULT_TABLE_READY_JS, timeout=RESULT_TABLE_WAIT_MS)
    except Exception as exc:
        logger.info("Result table was still short for %s: %s", url, exc)


def render_headless(url: str) -> str:
    """Render ``url`` in a headless browser.

    Uses the browser's own user agent. A 401/403 response is not retried.
    A race result page waits for the full finishing table before the HTML
    is read. A live track page sends ``Cache-Control: no-cache``.
    """
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
                raise AccessBlocked(status, PAGE_FORBIDDEN if status == 403 else PAGE_LOGIN)
            await_page_ready(page, url)
            html = str(page.content())
        finally:
            browser.close()
    if looks_like_login_wall(html):
        raise AccessBlocked(401, PAGE_LOGIN)
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
                return FallbackFetch(status="blocked", warning=_blocked_warning(self.site, blocked))
            except Exception as exc:
                logger.error("Page fetch failed for %s: %s", url, exc)
                return FallbackFetch(status="failed", warning=None)
        else:
            html = html_body

        if looks_like_login_wall(html):
            login = AccessBlocked(401, PAGE_LOGIN)
            return FallbackFetch(status="blocked", warning=_blocked_warning(self.site, login))

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
            return FallbackFetch(
                status="blocked",
                pages=pages,
                warning=_blocked_warning(self.site, blocked),
            )
        for endpoint in endpoints:
            try:
                body = self._get(endpoint, params=None, headers=headers, encoding=encoding)
            except AccessBlocked as blocked:
                return FallbackFetch(
                    status="blocked",
                    pages=pages,
                    warning=_blocked_warning(self.site, blocked),
                )
            except Exception as exc:
                logger.warning("Result endpoint %s failed: %s", endpoint, exc)
                continue
            if looks_like_login_wall(body):
                login = AccessBlocked(401, PAGE_LOGIN)
                return FallbackFetch(
                    status="blocked",
                    pages=pages,
                    warning=_blocked_warning(self.site, login),
                )
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
            return FallbackFetch(
                status="blocked",
                pages=pages,
                warning=_blocked_warning(self.site, rendered),
            )
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
                raise AccessBlocked(status, message) from exc
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


def _blocked_warning(site: str, blocked: AccessBlocked) -> str:
    if blocked.status == 403 or blocked.args[0] == PAGE_FORBIDDEN:
        return f"{site} returned 403"
    return f"{site} requires login"


def _with_query(url: str, params: dict[str, str] | None) -> str:
    if not params:
        return url
    from urllib.parse import urlencode

    join = "&" if "?" in url else "?"
    return f"{url}{join}{urlencode(params)}"
