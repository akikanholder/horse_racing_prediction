"""Optional Playwright fetch for JRA public pages.

The browser keeps its own user agent. A 401, 403, captcha, or login wall
stops the request. This module does not change the user agent, rotate
proxies, or retry a refusal. Playwright is optional: a missing browser
falls back to the HTTP client when the caller asked for that, and never
raises ImportError to the command.
"""

from __future__ import annotations

import html
import logging
import time
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from backend.scrapers.page_fallback import (
    HEADLESS_UNAVAILABLE,
    PAGE_FORBIDDEN,
    PAGE_LOGIN,
    AccessBlocked,
    HeadlessUnavailableError,
    looks_like_login_wall,
)
from backend.scrapers.track_freshness import cached_copy_warning, fresh_track_headers

logger = logging.getLogger(__name__)

FetchMode = Literal["html", "headless", "auto"]
HEADLESS_RENDER_FAILED = "headless render failed"
MIN_INTERVAL = 1.5

_shared: HeadlessSession | None = None


def close_shared() -> None:
    """Close the process-wide browser, if a JRA fetch opened one."""
    global _shared
    if _shared is not None:
        _shared.close()
        _shared = None


def shared_session() -> HeadlessSession:
    """One Chromium for JRA card, odds, payout, and track requests."""
    global _shared
    if _shared is None:
        _shared = HeadlessSession()
    return _shared


def _with_query(url: str, params: dict[str, str] | None) -> str:
    if not params:
        return url
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    pairs.extend((key, value) for key, value in params.items())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment))


def _denied(status: int | None, body: str) -> AccessBlocked | None:
    if status in {401, 403}:
        message = PAGE_FORBIDDEN if status == 403 else PAGE_LOGIN
        return AccessBlocked(status, message)
    if looks_like_login_wall(body):
        return AccessBlocked(401, PAGE_LOGIN)
    lowered = body.lower()
    if "captcha" in lowered or "access denied" in lowered:
        return AccessBlocked(403, PAGE_FORBIDDEN)
    return None


class HeadlessSession:
    """One Chromium. Own user agent. At least 1.5s between navigations."""

    def __init__(self, min_interval: float = MIN_INTERVAL) -> None:
        self.min_interval = min_interval
        self._last = 0.0
        self._playwright: Any = None
        self._browser: Any = None
        self.last_response_headers: dict[str, str] = {}
        self.served_cached_copy = False

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        encoding: str | None = None,
    ) -> str:
        """GET a public URL. Extra headers are ignored so the UA stays the browser's."""
        del headers, encoding
        target = _with_query(url, params)
        return self._run(lambda page: self._goto(page, target))

    def post(
        self,
        url: str,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        encoding: str | None = None,
    ) -> str:
        """POST the public form fields. JRA access pages send ``cname``."""
        del headers, encoding
        fields = data or {}
        return self._run(lambda page: self._submit(page, url, fields))

    def _browser_or_raise(self) -> Any:
        if self._browser is not None:
            return self._browser
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise HeadlessUnavailableError(HEADLESS_UNAVAILABLE) from exc
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
        except Exception as exc:
            self.close()
            raise HeadlessUnavailableError(HEADLESS_UNAVAILABLE) from exc
        return self._browser

    def _pause(self) -> None:
        elapsed = time.perf_counter() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def _run(self, navigate: Callable[[Any], None]) -> str:
        browser = self._browser_or_raise()
        self._pause()
        page = browser.new_page()
        try:
            navigate(page)
            response = getattr(page, "_last_response", None)
            status = response.status if response is not None else None
            self._remember_headers(response)
            body = str(page.content())
            self._count_json(page)
        finally:
            self._last = time.perf_counter()
            page.close()
        blocked = _denied(status if isinstance(status, int) else None, body)
        if blocked is not None:
            raise blocked
        if "パラメータエラー" in body:
            logger.warning("JRA rejected the cname. The error page is not parsed into prices.")
        return body

    @staticmethod
    def _count_json(page: Any) -> None:
        """Count JSON the rendered document already inlined. Prices stay in the DOM parsers."""
        try:
            payloads = page.evaluate(
                """() => {
                    const out = [];
                    for (const script of document.scripts) {
                        const type = (script.getAttribute('type') || '').toLowerCase();
                        const text = (script.textContent || '').trim();
                        if (!text) continue;
                        if (type.includes('json') || text.startsWith('{') || text.startsWith('[')) {
                            out.push(text);
                        }
                    }
                    return out.length;
                }"""
            )
        except Exception as exc:
            logger.debug("Headless JSON scan failed: %s", exc)
            return
        if isinstance(payloads, int) and payloads:
            logger.debug("Headless document inlined %s JSON script(s)", payloads)

    def _remember_headers(self, response: object) -> None:
        raw = getattr(response, "headers", None) if response is not None else None
        headers = {str(key).lower(): str(value) for key, value in dict(raw or {}).items()}
        self.last_response_headers = headers
        if cached_copy_warning(headers):
            self.served_cached_copy = True

    @staticmethod
    def _goto(page: Any, url: str) -> None:
        fresh = fresh_track_headers(url)
        if fresh and hasattr(page, "set_extra_http_headers"):
            page.set_extra_http_headers(fresh)
        response = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page._last_response = response

    @staticmethod
    def _submit(page: Any, url: str, fields: dict[str, str]) -> None:
        host = urlsplit(url).netloc
        inputs = "".join(
            f"<input name='{html.escape(key, quote=True)}' "
            f"value='{html.escape(str(value), quote=True)}'>"
            for key, value in fields.items()
        )
        page.goto("about:blank")
        page.set_content(
            f"<form id='f' method='POST' action='{html.escape(url, quote=True)}'>{inputs}</form>"
        )
        page.locator("#f").wait_for(state="attached")
        # set_content is a navigation. Wait for the form action, not the form document.
        with page.expect_navigation(
            wait_until="domcontentloaded",
            timeout=30_000,
            url=lambda target: bool(host) and host in target,
        ) as navigation:
            page.locator("#f").evaluate("form => form.submit()")
        page._last_response = navigation.value


class HeadlessTransport:
    """Send JRA requests through the browser when ``fetch_mode`` asks for it.

    ``html`` uses the inner client. ``headless`` and ``auto`` try the browser
    first. A missing browser or a render error falls back to the inner client.
    ``headless`` records a warning for that fallback. ``auto`` does not.
    A refusal is not sent again through HTTP.
    """

    def __init__(
        self,
        inner: object,
        *,
        mode_of: Callable[[], str],
        session: object | None = None,
    ) -> None:
        self._inner = inner
        self._mode_of = mode_of
        self._session = session
        self.used_headless = False
        self.fallback_warning: str | None = None
        self.last_response_headers: dict[str, str] = {}
        self.served_cached_copy = False

    def _mode(self) -> str:
        mode = self._mode_of()
        if mode in {"headless", "auto"}:
            return mode
        return "html"

    def _note(self, message: str) -> None:
        if self.fallback_warning is None:
            self.fallback_warning = message
        elif message not in self.fallback_warning:
            self.fallback_warning = f"{self.fallback_warning}; {message}"

    def _browser(self) -> object:
        if self._session is None:
            self._session = shared_session()
        return self._session

    def _inner_call(self, method: str, url: str, **kwargs: object) -> str:
        fn = getattr(self._inner, method)
        body = fn(url, **kwargs)
        if not isinstance(body, str):
            raise TypeError("JRA HTTP client did not return text")
        return body

    def _call(self, method: str, url: str, **kwargs: object) -> str:
        if self._mode() == "html":
            self.used_headless = False
            return self._inner_call(method, url, **kwargs)
        try:
            session = self._browser()
            fn = getattr(session, method)
            body = fn(url, **kwargs)
        except AccessBlocked:
            self.used_headless = False
            raise
        except HeadlessUnavailableError:
            self.used_headless = False
            if self._mode() == "headless":
                self._note(HEADLESS_UNAVAILABLE)
            return self._inner_call(method, url, **kwargs)
        except Exception as exc:
            logger.warning("Headless JRA fetch failed for %s: %s", url, exc)
            self.used_headless = False
            if self._mode() == "headless":
                self._note(HEADLESS_RENDER_FAILED)
            return self._inner_call(method, url, **kwargs)
        if not isinstance(body, str):
            raise TypeError("Headless JRA fetch did not return text")
        self.used_headless = True
        return body

    def _capture_headers(self) -> None:
        if self.used_headless and self._session is not None:
            source = self._session
        else:
            source = self._inner
        headers = getattr(source, "last_response_headers", None)
        self.last_response_headers = dict(headers or {})
        if getattr(source, "served_cached_copy", False):
            self.served_cached_copy = True

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        encoding: str | None = None,
    ) -> str:
        try:
            return self._call("get", url, params=params, headers=headers, encoding=encoding)
        finally:
            self._capture_headers()

    def post(
        self,
        url: str,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        encoding: str | None = None,
    ) -> str:
        try:
            return self._call("post", url, data=data, headers=headers, encoding=encoding)
        finally:
            self._capture_headers()

    def close(self) -> None:
        closer = getattr(self._inner, "close", None)
        if callable(closer):
            closer()
