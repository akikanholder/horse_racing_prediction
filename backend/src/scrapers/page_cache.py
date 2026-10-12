"""Local cache and a pause between public-page requests.

Date ranges call sites one request at a time. A cached page is not fetched
again. HTTP 403 and a login wall are not stored. This client does not change
the User-Agent or send credentials.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

from backend.scrapers.page_fallback import looks_like_login_wall
from backend.utils.http_client import HTTPClient

logger = logging.getLogger(__name__)


def default_page_cache_dir() -> Path:
    """backend/.cache/pages. The directory is created on the first write."""
    return Path(__file__).resolve().parents[2] / ".cache" / "pages"


class CachingHTTPClient(HTTPClient):
    """HTTP client that stores successful public pages on disk.

    A cache hit returns the stored body and does not wait. A miss uses the
    normal rate limit, then stores the body with the time it was fetched.
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        *,
        transport: object | None = None,
        min_request_interval: float = 1.5,
    ):
        super().__init__(min_request_interval=min_request_interval)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else default_page_cache_dir()
        self._transport = transport

    def cached_fetched_at(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        data: dict | None = None,
    ) -> datetime | None:
        """When this URL was fetched. None when it is not on disk."""
        path = self._path(self._cache_key(method, url, params, data))
        payload = self._load(path)
        if payload is None:
            return None
        raw = payload.get("fetched_at")
        if not isinstance(raw, str):
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        encoding: str | None = None,
    ) -> str:
        key = self._cache_key("GET", url, params, None)
        cached = self._body(key)
        if cached is not None:
            return cached
        body = self._fetch("get", url, params=params, data=None, headers=headers, encoding=encoding)
        self._store(key, url, body)
        return body

    def post(
        self,
        url: str,
        data: dict | None = None,
        headers: dict | None = None,
        encoding: str | None = None,
    ) -> str:
        key = self._cache_key("POST", url, None, data)
        cached = self._body(key)
        if cached is not None:
            return cached
        body = self._fetch("post", url, params=None, data=data, headers=headers, encoding=encoding)
        self._store(key, url, body)
        return body

    def _fetch(
        self,
        method: str,
        url: str,
        *,
        params: dict | None,
        data: dict | None,
        headers: dict | None,
        encoding: str | None,
    ) -> str:
        if self._transport is None:
            if method == "post":
                return super().post(url, data=data, headers=headers, encoding=encoding)
            return super().get(url, params=params, headers=headers, encoding=encoding)
        self._wait_for_rate_limit()
        caller = getattr(self._transport, method)
        if method == "post":
            body = caller(url, data=data, headers=headers, encoding=encoding)
        else:
            body = caller(url, params=params, headers=headers, encoding=encoding)
        if not isinstance(body, str):
            raise TypeError("http transport did not return text")
        self._last_request_time = datetime.now().timestamp()
        return body

    def _store(self, key: str, url: str, body: str) -> None:
        if not body or looks_like_login_wall(body):
            return
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "url": url,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "body": body,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _body(self, key: str) -> str | None:
        payload = self._load(self._path(key))
        if payload is None:
            return None
        body = payload.get("body")
        if not isinstance(body, str) or not body:
            return None
        return body

    def _load(self, path: Path) -> dict[str, object] | None:
        if not path.is_file():
            return None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Page cache %s could not be read: %s", path, exc)
            return None
        if not isinstance(loaded, dict):
            return None
        return loaded

    def _path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    @staticmethod
    def _cache_key(
        method: str,
        url: str,
        params: dict | None,
        data: dict | None,
    ) -> str:
        raw = json.dumps(
            {"method": method, "url": url, "params": params or {}, "data": data or {}},
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
