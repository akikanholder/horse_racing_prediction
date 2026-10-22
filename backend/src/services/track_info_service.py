"""Track information service that aggregates data from multiple sources."""

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime

from backend.models.track import CourseInfo, TrackInfo, empty_track_info
from backend.scrapers.jra_headless import close_shared
from backend.scrapers.jra_track_scraper import JRATrackScraper
from backend.scrapers.netkeiba_track_scraper import NetkeibaTrackScraper
from backend.scrapers.track_freshness import (
    apply_measurement_freshness,
    needs_fresh_wait,
    parse_race_day,
    polite_interval,
    require_finite_seconds,
    track_blocked,
    wait_budget_spent,
)

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
        jra_scraper: JRATrackScraper | None = None,
        netkeiba_scraper: NetkeibaTrackScraper | None = None,
        fetch_mode: str = "html",
    ):
        """Initialize the service with scrapers.

        Args:
            jra_scraper: JRA scraper instance (created if not provided)
            netkeiba_scraper: Netkeiba scraper instance (created if not provided)
            fetch_mode: ``html``, ``headless``, or ``auto`` for the JRA baba pages
        """
        if fetch_mode not in {"html", "headless", "auto"}:
            fetch_mode = "html"
        self.fetch_mode = fetch_mode
        self._jra_scraper = jra_scraper
        self._netkeiba_scraper = netkeiba_scraper
        self._owns_jra_scraper = jra_scraper is None
        self._owns_netkeiba_scraper = netkeiba_scraper is None

    @property
    def jra_scraper(self) -> JRATrackScraper:
        """Get JRA scraper, creating if necessary."""
        if self._jra_scraper is None:
            self._jra_scraper = JRATrackScraper(fetch_mode=self.fetch_mode)  # type: ignore[arg-type]
        return self._jra_scraper

    @property
    def netkeiba_scraper(self) -> NetkeibaTrackScraper:
        """Get Netkeiba scraper, creating if necessary."""
        if self._netkeiba_scraper is None:
            self._netkeiba_scraper = NetkeibaTrackScraper()
        return self._netkeiba_scraper

    def get_track_info_from_jra(
        self,
        racecourse: str | None = None,
        *,
        race_date: date | None = None,
        today: date | None = None,
        race_date_warning: str | None = None,
    ) -> TrackInfo:
        """Get track information from JRA.

        Args:
            racecourse: Racecourse name in Japanese (e.g., '中山') or
                       JRA code (e.g., 'nakayama')

        Returns:
            TrackInfo from JRA
        """
        jra_code = self._get_jra_code(racecourse)
        return self.jra_scraper.scrape(
            jra_code,
            race_date=race_date,
            today=today,
            race_date_warning=race_date_warning,
        )

    def get_track_info_from_netkeiba(
        self,
        racecourse: str | None = None,
        date: str | None = None,
        *,
        race_date: date | None = None,
        today: date | None = None,
        race_date_warning: str | None = None,
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
        return self.netkeiba_scraper.scrape(
            netkeiba_id,
            date,
            race_date=race_date,
            today=today,
            race_date_warning=race_date_warning,
        )

    def get_track_info(
        self,
        racecourse: str | None = None,
        date: str | None = None,
        sources: list[str] | None = None,
        merge: bool = False,
        fetch_mode: str | None = None,
        wait_fresh: bool = False,
        retry_interval: float = 60,
        max_wait: float = 600,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        today: date | None = None,
    ) -> dict[str, TrackInfo]:
        """Get track information from specified sources.

        Args:
            racecourse: Racecourse name in Japanese or source-specific code
            date: Date in YYYYMMDD format (for Netkeiba). Also the race date
                  used to judge a stale measurement.
            sources: List of sources to use ('jra', 'netkeiba').
                    If None, both sources are used.
            merge: If True, returns a merged TrackInfo under 'merged' key
            wait_fresh: Refetch while a printed measurement is not the race date.
            retry_interval: Seconds between refetches. Raised to 60 when shorter.
            max_wait: Stop once another wait would pass this many seconds.
            today: Asia/Tokyo date used for ``is_today``. Defaults to today.

        Returns:
            Dictionary mapping source name to TrackInfo
        """
        if sources is None:
            sources = ["jra", "netkeiba"]

        with self._using(fetch_mode):
            return self._track_info(
                racecourse,
                date,
                sources,
                merge,
                wait_fresh=wait_fresh,
                retry_interval=retry_interval,
                max_wait=max_wait,
                sleeper=sleeper,
                clock=clock,
                today=today,
            )

    def _track_info(
        self,
        racecourse: str | None,
        date: str | None,
        sources: list[str],
        merge: bool,
        *,
        wait_fresh: bool = False,
        retry_interval: float = 60,
        max_wait: float = 600,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        today: date | None = None,
    ) -> dict[str, TrackInfo]:
        race_day, race_warning = parse_race_day(date)
        if wait_fresh:
            retry_interval = require_finite_seconds(retry_interval, "retry_interval")
            max_wait = require_finite_seconds(max_wait, "max_wait")
        interval = polite_interval(retry_interval)
        mono = clock or time.monotonic
        pause = sleeper or time.sleep
        started = mono()
        while True:
            results = self._fetch_track_info(
                racecourse,
                date,
                sources,
                merge,
                race_day=race_day,
                race_warning=race_warning,
                today=today,
            )
            if not wait_fresh or any(track_blocked(info) for info in results.values()):
                return results
            if not needs_fresh_wait(results, race_date=race_day, today=today):
                return results
            if wait_budget_spent(started, mono(), interval, max_wait):
                return results
            pause(interval)

    def _fetch_track_info(
        self,
        racecourse: str | None,
        date: str | None,
        sources: list[str],
        merge: bool,
        *,
        race_day: date | None,
        race_warning: str | None,
        today: date | None,
    ) -> dict[str, TrackInfo]:
        results: dict[str, TrackInfo] = {}
        errors = {}
        fresh = {
            "race_date": race_day,
            "today": today,
            "race_date_warning": race_warning,
        }

        if "jra" in sources:
            try:
                results["jra"] = self.get_track_info_from_jra(racecourse, **fresh)
            except Exception as e:
                logger.warning(f"Failed to get JRA track info: {e}")
                errors["jra"] = str(e)
                results["jra"] = apply_measurement_freshness(
                    empty_track_info("JRA", "JRA baba fetch failed"),
                    race_date=race_day,
                    today=today,
                    race_date_warning=race_warning,
                )

        # JRA 403, a foreign tab, or a page with no cushion number falls back to
        # netkeiba's public track page for the same venue and date. The JRA
        # payload stays the failure. Values copied from netkeiba name that source.
        if "netkeiba" in sources or _jra_needs_netkeiba(results.get("jra")):
            try:
                results["netkeiba"] = self.get_track_info_from_netkeiba(racecourse, date, **fresh)
            except Exception as e:
                logger.warning(f"Failed to get Netkeiba track info: {e}")
                errors["netkeiba"] = str(e)
                results["netkeiba"] = apply_measurement_freshness(
                    empty_track_info("Netkeiba", "netkeiba track fetch failed"),
                    race_date=race_day,
                    today=today,
                    race_date_warning=race_warning,
                )

        jra_info = results.get("jra")
        netkeiba_info = results.get("netkeiba")
        if (
            merge
            and jra_info is not None
            and netkeiba_info is not None
            and (jra_info.racecourse or netkeiba_info.racecourse)
        ):
            results["merged"] = apply_measurement_freshness(
                self._merge_track_info(jra_info, netkeiba_info),
                race_date=race_day,
                today=today,
                race_date_warning=race_warning,
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

    def _get_jra_code(self, racecourse: str | None) -> str | None:
        """Convert racecourse identifier to JRA code."""
        if racecourse is None:
            return None

        if racecourse in self.RACECOURSE_CODE_MAPPING:
            return self.RACECOURSE_CODE_MAPPING[racecourse]["jra"]

        for codes in self.RACECOURSE_CODE_MAPPING.values():
            if codes["jra"] == racecourse:
                return racecourse

        return racecourse

    def _get_netkeiba_id(self, racecourse: str | None) -> str | None:
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
        self, jra_info: TrackInfo | None, netkeiba_info: TrackInfo | None
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

        weather = jra_info.weather
        weather_source = jra_info.weather_source
        weather_fetched_at = jra_info.weather_fetched_at
        if jra_info.weather.value == "不明":
            weather = netkeiba_info.weather
            weather_source = netkeiba_info.weather_source
            weather_fetched_at = netkeiba_info.weather_fetched_at
        post_time = jra_info.post_time or netkeiba_info.post_time
        post_source = (
            jra_info.post_time_source if jra_info.post_time else netkeiba_info.post_time_source
        )
        post_fetched = (
            jra_info.post_time_fetched_at
            if jra_info.post_time
            else netkeiba_info.post_time_fetched_at
        )
        return TrackInfo(
            racecourse=jra_info.racecourse or netkeiba_info.racecourse,
            date=jra_info.date or netkeiba_info.date,
            weather=weather,
            weather_source=weather_source,
            weather_fetched_at=weather_fetched_at,
            turf=merged_turf,
            dirt=merged_dirt,
            grass_info=jra_info.grass_info or netkeiba_info.grass_info,
            weekly_info=jra_info.weekly_info or netkeiba_info.weekly_info,
            source="JRA+Netkeiba",
            warning=join_track_warnings(jra_info.warning, netkeiba_info.warning),
            post_time=post_time,
            post_time_source=post_source,
            post_time_fetched_at=post_fetched,
            scraped_at=datetime.now(),
        )

    def _merge_course_info(
        self, primary: CourseInfo | None, secondary: CourseInfo | None
    ) -> CourseInfo | None:
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
        if merged_cushion is None or merged_cushion.value is None:
            if secondary.cushion is not None and secondary.cushion.value is not None:
                merged_cushion = secondary.cushion

        use_secondary_going = primary.condition.value == "不明"
        return CourseInfo(
            course_type=primary.course_type,
            condition=secondary.condition if use_secondary_going else primary.condition,
            condition_source=(
                secondary.condition_source if use_secondary_going else primary.condition_source
            ),
            condition_fetched_at=(
                secondary.condition_fetched_at
                if use_secondary_going
                else primary.condition_fetched_at
            ),
            moisture=merged_moisture,
            cushion=merged_cushion,
        )

    def close(self):
        """Close scraper sessions."""
        if self._owns_jra_scraper and self._jra_scraper:
            self._jra_scraper.close()
        if self._owns_netkeiba_scraper and self._netkeiba_scraper:
            self._netkeiba_scraper.close()
        if self._owns_jra_scraper:
            close_shared()

    @contextmanager
    def _using(self, fetch_mode: str | None) -> Iterator[None]:
        chosen = fetch_mode if fetch_mode in {"html", "headless", "auto"} else self.fetch_mode
        previous = self.fetch_mode
        scraper = self._jra_scraper
        previous_scraper = getattr(scraper, "fetch_mode", None)
        self.fetch_mode = chosen
        if scraper is not None and hasattr(scraper, "fetch_mode"):
            scraper.fetch_mode = chosen  # type: ignore[assignment]
        try:
            yield
        finally:
            self.fetch_mode = previous
            if (
                self._jra_scraper is not None
                and previous_scraper is not None
                and hasattr(self._jra_scraper, "fetch_mode")
            ):
                self._jra_scraper.fetch_mode = previous_scraper  # type: ignore[assignment]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


def _jra_needs_netkeiba(info: TrackInfo | None) -> bool:
    """True when JRA did not publish a cushion number for the requested meeting."""
    if info is None or info.racecourse is None:
        return True
    cushion = info.turf.cushion if info.turf is not None else None
    return cushion is None or cushion.value is None


def join_track_warnings(*parts: str | None) -> str | None:
    """Keep both reasons when a merged track still has a hole."""
    items = [part for part in parts if part]
    if not items:
        return None
    return "; ".join(dict.fromkeys(items))
