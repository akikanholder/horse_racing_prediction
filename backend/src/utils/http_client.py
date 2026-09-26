"""HTTP client with retry logic and rate limiting."""

import asyncio
import logging
import random
import time

import httpx
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class HTTPClient:
    """HTTP client with retry logic and rate limiting."""

    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }

    def __init__(
        self,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        min_request_interval: float = 1.0,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
    ):
        """Initialize HTTP client.

        Args:
            max_retries: Maximum number of retry attempts
            backoff_factor: Backoff factor for retries
            min_request_interval: Minimum seconds between requests
            timeout: Request timeout in seconds
            headers: Extra headers stored on the Session and sent with every request
        """
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.min_request_interval = min_request_interval
        self.timeout = timeout
        self._extra_headers = dict(headers or {})
        self._last_request_time: float = 0
        self._session: requests.Session | None = None

    def _get_session(self) -> requests.Session:
        """Get or create requests session with retry configuration."""
        if self._session is None:
            self._session = requests.Session()
            retry_strategy = Retry(
                total=self.max_retries,
                backoff_factor=self.backoff_factor,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["GET", "HEAD", "OPTIONS", "POST"],
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            self._session.mount("http://", adapter)
            self._session.mount("https://", adapter)
            self._session.headers.update(self.DEFAULT_HEADERS)
            self._session.headers.update(self._extra_headers)
        return self._session

    def _headers_for(self, headers: dict | None) -> dict[str, str]:
        """Merge default, session, and per-request headers and keep them on the Session."""
        merged = {**self.DEFAULT_HEADERS, **self._extra_headers}
        if headers:
            merged.update(headers)
        session = self._get_session()
        session.headers.update(merged)
        return merged

    def _wait_for_rate_limit(self) -> None:
        """Wait to respect rate limiting."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.min_request_interval:
            sleep_time = self.min_request_interval - elapsed + random.uniform(0, 0.5)
            time.sleep(sleep_time)

    def get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        encoding: str | None = None,
    ) -> str:
        """Make GET request and return response text.

        Args:
            url: URL to request
            params: Query parameters
            headers: Additional headers
            encoding: Response encoding (auto-detected if not specified)

        Returns:
            Response text content
        """
        self._wait_for_rate_limit()

        session = self._get_session()
        request_headers = self._headers_for(headers)

        try:
            logger.debug(f"Requesting: {url}")
            response = session.get(
                url,
                params=params,
                headers=request_headers,
                timeout=self.timeout,
            )
            response.raise_for_status()

            if encoding:
                response.encoding = encoding
            elif response.encoding is None:
                response.encoding = "utf-8"

            self._last_request_time = time.time()
            return response.text

        except requests.RequestException as e:
            logger.error(f"Request failed for {url}: {e}")
            raise

    def post(
        self,
        url: str,
        data: dict | None = None,
        headers: dict | None = None,
        encoding: str | None = None,
    ) -> str:
        """Make POST request and return response text.

        Args:
            url: URL to request
            data: Form body. JRA accessD expects ``cname``.
            headers: Additional headers
            encoding: Response encoding (auto-detected if not specified)

        Returns:
            Response text content
        """
        self._wait_for_rate_limit()

        session = self._get_session()
        request_headers = self._headers_for(headers)

        try:
            logger.debug(f"Posting: {url}")
            response = session.post(
                url,
                data=data,
                headers=request_headers,
                timeout=self.timeout,
            )
            response.raise_for_status()

            if encoding:
                response.encoding = encoding
            elif response.encoding is None:
                response.encoding = "utf-8"

            self._last_request_time = time.time()
            return response.text

        except requests.RequestException as e:
            logger.error(f"Request failed for {url}: {e}")
            raise

    def close(self) -> None:
        """Close the session."""
        if self._session:
            self._session.close()
            self._session = None


class AsyncHTTPClient:
    """Async HTTP client with retry logic and rate limiting."""

    DEFAULT_HEADERS = HTTPClient.DEFAULT_HEADERS

    def __init__(
        self,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        min_request_interval: float = 1.0,
        timeout: float = 30.0,
    ):
        """Initialize async HTTP client."""
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.min_request_interval = min_request_interval
        self.timeout = timeout
        self._last_request_time: float = 0
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create async client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=self.DEFAULT_HEADERS,
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
            )
        return self._client

    async def _wait_for_rate_limit(self) -> None:
        """Wait to respect rate limiting."""
        async with self._lock:
            elapsed = time.time() - self._last_request_time
            if elapsed < self.min_request_interval:
                sleep_time = self.min_request_interval - elapsed + random.uniform(0, 0.5)
                await asyncio.sleep(sleep_time)
            self._last_request_time = time.time()

    async def get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> str:
        """Make async GET request.

        Args:
            url: URL to request
            params: Query parameters
            headers: Additional headers

        Returns:
            Response text content
        """
        await self._wait_for_rate_limit()

        client = await self._get_client()
        request_headers = {**self.DEFAULT_HEADERS}
        if headers:
            request_headers.update(headers)

        for attempt in range(self.max_retries + 1):
            try:
                logger.debug(f"Async requesting: {url} (attempt {attempt + 1})")
                response = await client.get(url, params=params, headers=request_headers)
                response.raise_for_status()
                return response.text

            except httpx.HTTPError as e:
                if attempt == self.max_retries:
                    logger.error(f"Async request failed for {url}: {e}")
                    raise
                wait_time = self.backoff_factor * (2**attempt)
                logger.warning(
                    f"Request failed, retrying in {wait_time}s: {e}"
                )
                await asyncio.sleep(wait_time)

        raise RuntimeError("Unexpected end of retry loop")

    async def close(self) -> None:
        """Close the async client."""
        if self._client:
            await self._client.aclose()
            self._client = None
