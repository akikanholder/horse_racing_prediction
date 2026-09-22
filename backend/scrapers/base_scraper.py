"""Base scraper class."""

import logging
from abc import ABC

from bs4 import BeautifulSoup

from backend.utils.http_client import HTTPClient

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """Abstract base class for scrapers."""

    BASE_URL: str = ""
    ENCODING: str = "utf-8"

    def __init__(self, http_client: HTTPClient | None = None):
        """Initialize scraper.

        Args:
            http_client: HTTP client instance (creates new one if not provided)
        """
        self.http_client = http_client or HTTPClient()

    def fetch_page(self, url: str, params: dict | None = None) -> BeautifulSoup:
        """Fetch page and return parsed BeautifulSoup object.

        Args:
            url: URL to fetch
            params: Optional query parameters

        Returns:
            Parsed BeautifulSoup object
        """
        html = self.http_client.get(url, params=params, encoding=self.ENCODING)
        return BeautifulSoup(html, "lxml")

    def build_url(self, path: str) -> str:
        """Build full URL from path.

        Args:
            path: URL path

        Returns:
            Full URL
        """
        if path.startswith("http"):
            return path
        return f"{self.BASE_URL.rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def clean_text(text: str | None) -> str:
        """Clean text by removing extra whitespace.

        Args:
            text: Text to clean

        Returns:
            Cleaned text
        """
        if text is None:
            return ""
        return " ".join(text.split())

    @staticmethod
    def extract_number(text: str) -> int | None:
        """Extract integer from text.

        Args:
            text: Text containing number

        Returns:
            Extracted integer or None
        """
        import re

        match = re.search(r"\d+", text)
        if match:
            return int(match.group())
        return None

    @staticmethod
    def extract_float(text: str) -> float | None:
        """Extract float from text.

        Args:
            text: Text containing number

        Returns:
            Extracted float or None
        """
        import re

        match = re.search(r"[\d.]+", text)
        if match:
            try:
                return float(match.group())
            except ValueError:
                pass
        return None

    def close(self) -> None:
        """Close the scraper and release resources."""
        self.http_client.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
        return False
