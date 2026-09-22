"""Track information service that aggregates data from multiple sources."""

import logging
from datetime import datetime
from typing import Optional

from .jra_scraper import JRAScraper
from .models import CourseInfo, TrackInfo
from .netkeiba_scraper import NetkeibaScraper

logger = logging.getLogger(__name__)


class TrackInfoService:
    """Service for fetching and aggregating track information from multiple sources.

    This service provides a unified interface for accessing track condition data
    from both JRA and Netkeiba. It can merge data from multiple sources to provide
    more complete information.
    """

    RACECOURSE_CODE_MAPPING = {
        "札幌": {"jra": "sapporo", "netkeiba": "01"},
        "函館": {"jra": "hakodate", "netkeiba": "02"},
        "福島": {"jra": "fukushima", "netkeiba": "03"},
        "新潟": {"jra": "niigata", "netkeiba": "04"},
        "東京": {"jra": "tokyo", "netkeiba": "05"},
        "中山": {"jra": "nakayama", "netkeiba": "06"},
        "中京": {"jra": "chukyo", "netkeiba": "07"},
        "京都": {"jra": "kyoto", "netkeiba": "08"},
        "阪神": {"jra": "hanshin", "netkeiba": "09"},
        "小倉": {"jra": "kokura", "netkeiba": "10"},
    }

    def __init__(
        self,
        jra_scraper: Optional[JRAScraper] = None,
        netkeiba_scraper: Optional[NetkeibaScraper] = None,
    ):
        """Initialize the service with scrapers.

        Args:
            jra_scraper: JRA scraper instance (created if not provided)
            netkeiba_scraper: Netkeiba scraper instance (created if not provided)
        """
        self._jra_scraper = jra_scraper
        self._netkeiba_scraper = netkeiba_scraper
        self._owns_jra_scraper = jra_scraper is None
        self._owns_netkeiba_scraper = netkeiba_scraper is None

    @property
    def jra_scraper(self) -> JRAScraper:
        """Get JRA scraper, creating if necessary."""
        if self._jra_scraper is None:
            self._jra_scraper = JRAScraper()
        return self._jra_scraper

    @property
    def netkeiba_scraper(self) -> NetkeibaScraper:
        """Get Netkeiba scraper, creating if necessary."""
        if self._netkeiba_scraper is None:
            self._netkeiba_scraper = NetkeibaScraper()
        return self._netkeiba_scraper

    def get_track_info_from_jra(
        self, racecourse: Optional[str] = None
    ) -> TrackInfo:
        """Get track information from JRA.

        Args:
            racecourse: Racecourse name in Japanese (e.g., '中山') or
                       JRA code (e.g., 'nakayama')

        Returns:
            TrackInfo from JRA
        """
        jra_code = self._get_jra_code(racecourse)
        return self.jra_scraper.scrape(jra_code)

    def get_track_info_from_netkeiba(
        self, racecourse: Optional[str] = None, date: Optional[str] = None
    ) -> TrackInfo:
        """Get track information from Netkeiba.

        Args:
            racecourse: Racecourse name in Japanese (e.g., '中山') or
                       Netkeiba ID (e.g., '06')
            date: Date in YYYYMMDD format

        Returns:
            TrackInfo from Netkeiba
        """
        netkeiba_id = self._get_netkeiba_id(racecourse)
        return self.netkeiba_scraper.scrape(netkeiba_id, date)

    def get_track_info(
        self,
        racecourse: Optional[str] = None,
        date: Optional[str] = None,
        sources: Optional[list[str]] = None,
        merge: bool = False,
    ) -> dict[str, TrackInfo]:
        """Get track information from specified sources.

        Args:
            racecourse: Racecourse name in Japanese or source-specific code
            date: Date in YYYYMMDD format (for Netkeiba)
            sources: List of sources to use ('jra', 'netkeiba').
                    If None, both sources are used.
            merge: If True, returns a merged TrackInfo under 'merged' key

        Returns:
            Dictionary mapping source name to TrackInfo
        """
        if sources is None:
            sources = ["jra", "netkeiba"]

        results = {}
        errors = {}

        if "jra" in sources:
            try:
                results["jra"] = self.get_track_info_from_jra(racecourse)
            except Exception as e:
                logger.warning(f"Failed to get JRA track info: {e}")
                errors["jra"] = str(e)

        if "netkeiba" in sources:
            try:
                results["netkeiba"] = self.get_track_info_from_netkeiba(
                    racecourse, date
                )
            except Exception as e:
                logger.warning(f"Failed to get Netkeiba track info: {e}")
                errors["netkeiba"] = str(e)

        if merge and len(results) > 1:
            results["merged"] = self._merge_track_info(
                results.get("jra"), results.get("netkeiba")
            )

        if errors:
            logger.info(f"Errors during scraping: {errors}")

        return results

    def get_all_racecourses(self) -> list[dict]:
        """Get list of all available racecourses with their codes.

        Returns:
            List of dictionaries with racecourse information
        """
        return [
            {
                "name": name,
                "jra_code": codes["jra"],
                "netkeiba_id": codes["netkeiba"],
            }
            for name, codes in self.RACECOURSE_CODE_MAPPING.items()
        ]

    def _get_jra_code(self, racecourse: Optional[str]) -> Optional[str]:
        """Convert racecourse identifier to JRA code."""
        if racecourse is None:
            return None

        if racecourse in self.RACECOURSE_CODE_MAPPING:
            return self.RACECOURSE_CODE_MAPPING[racecourse]["jra"]

        for codes in self.RACECOURSE_CODE_MAPPING.values():
            if codes["jra"] == racecourse:
                return racecourse

        return racecourse

    def _get_netkeiba_id(self, racecourse: Optional[str]) -> Optional[str]:
        """Convert racecourse identifier to Netkeiba ID."""
        if racecourse is None:
            return None

        if racecourse in self.RACECOURSE_CODE_MAPPING:
            return self.RACECOURSE_CODE_MAPPING[racecourse]["netkeiba"]

        for codes in self.RACECOURSE_CODE_MAPPING.values():
            if codes["netkeiba"] == racecourse:
                return racecourse

        return racecourse

    def _merge_track_info(
        self, jra_info: Optional[TrackInfo], netkeiba_info: Optional[TrackInfo]
    ) -> TrackInfo:
        """Merge track information from JRA and Netkeiba.

        JRA is considered the authoritative source, with Netkeiba data
        filling in gaps where JRA data is missing.

        Args:
            jra_info: TrackInfo from JRA
            netkeiba_info: TrackInfo from Netkeiba

        Returns:
            Merged TrackInfo
        """
        if jra_info is None and netkeiba_info is None:
            raise ValueError("At least one source must provide data")

        if jra_info is None:
            return netkeiba_info
        if netkeiba_info is None:
            return jra_info

        merged_turf = self._merge_course_info(jra_info.turf, netkeiba_info.turf)
        merged_dirt = self._merge_course_info(jra_info.dirt, netkeiba_info.dirt)

        return TrackInfo(
            racecourse=jra_info.racecourse or netkeiba_info.racecourse,
            date=jra_info.date,
            weather=jra_info.weather
            if jra_info.weather.value != "不明"
            else netkeiba_info.weather,
            turf=merged_turf,
            dirt=merged_dirt,
            grass_info=jra_info.grass_info,
            weekly_info=jra_info.weekly_info,
            source="JRA+Netkeiba",
            scraped_at=datetime.now(),
        )

    def _merge_course_info(
        self, primary: Optional[CourseInfo], secondary: Optional[CourseInfo]
    ) -> Optional[CourseInfo]:
        """Merge course information, preferring primary (JRA) data."""
        if primary is None:
            return secondary
        if secondary is None:
            return primary

        merged_moisture = primary.moisture
        if merged_moisture is None:
            merged_moisture = secondary.moisture
        elif secondary.moisture:
            if merged_moisture.goal_front is None:
                merged_moisture = merged_moisture.model_copy(
                    update={"goal_front": secondary.moisture.goal_front}
                )
            if merged_moisture.fourth_corner is None:
                merged_moisture = merged_moisture.model_copy(
                    update={"fourth_corner": secondary.moisture.fourth_corner}
                )

        merged_cushion = primary.cushion
        if merged_cushion is None:
            merged_cushion = secondary.cushion

        return CourseInfo(
            course_type=primary.course_type,
            condition=primary.condition
            if primary.condition.value != "不明"
            else secondary.condition,
            moisture=merged_moisture,
            cushion=merged_cushion,
        )

    def close(self):
        """Close scraper sessions."""
        if self._owns_jra_scraper and self._jra_scraper:
            self._jra_scraper.close()
        if self._owns_netkeiba_scraper and self._netkeiba_scraper:
            self._netkeiba_scraper.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
