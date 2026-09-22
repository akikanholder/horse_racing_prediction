"""Base scraper class with common functionality."""

import logging
from abc import ABC, abstractmethod
from typing import Optional

import requests
from bs4 import BeautifulSoup

from .models import TrackInfo

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """Base class for track information scrapers."""

    DEFAULT_TIMEOUT = 30
    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        headers: Optional[dict] = None,
    ):
        """Initialize the scraper.

        Args:
            timeout: Request timeout in seconds
            headers: Custom headers to use for requests
        """
        self.timeout = timeout
        self.headers = headers or self.DEFAULT_HEADERS
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def fetch_page(self, url: str) -> BeautifulSoup:
        """Fetch a page and return a BeautifulSoup object.

        Args:
            url: URL to fetch

        Returns:
            BeautifulSoup object of the page

        Raises:
            requests.RequestException: If the request fails
        """
        logger.info(f"Fetching URL: {url}")
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
        return BeautifulSoup(response.text, "lxml")

    @abstractmethod
    def get_base_url(self) -> str:
        """Return the base URL for this scraper."""
        pass

    @abstractmethod
    def scrape(self, **kwargs) -> TrackInfo:
        """Scrape track information.

        Returns:
            TrackInfo object with the scraped data
        """
        pass

    def close(self):
        """Close the session."""
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
