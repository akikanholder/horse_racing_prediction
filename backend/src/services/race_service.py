"""Race service for aggregating data from multiple sources."""

import logging
from datetime import datetime

from backend.models.course import Course, CourseCondition, WeeklyVenueSchedule
from backend.models.horse import Horse, HorseBasicInfo
from backend.models.race import (
    Race,
    RaceCard,
    RaceSchedule,
    RaceWithResults,
)
from backend.scrapers.jra_scraper import JRAScraper
from backend.scrapers.netkeiba_scraper import NetkeibaScraper

logger = logging.getLogger(__name__)


class RaceService:
    """Service for aggregating race data from multiple sources."""

    def __init__(self):
        """Initialize race service."""
        self._jra_scraper: JRAScraper | None = None
        self._netkeiba_scraper: NetkeibaScraper | None = None

    @property
    def jra_scraper(self) -> JRAScraper:
        """Get or create JRA scraper."""
        if self._jra_scraper is None:
            self._jra_scraper = JRAScraper()
        return self._jra_scraper

    @property
    def netkeiba_scraper(self) -> NetkeibaScraper:
        """Get or create Netkeiba scraper."""
        if self._netkeiba_scraper is None:
            self._netkeiba_scraper = NetkeibaScraper()
        return self._netkeiba_scraper

    def get_weekly_races(self) -> RaceSchedule:
        """Get this week's race schedule from all sources.

        Combines data from JRA and netkeiba for comprehensive coverage.

        Returns:
            RaceSchedule with all available races
        """
        all_races: dict[str, Race] = {}

        try:
            jra_schedule = self.jra_scraper.get_weekly_race_schedule()
            for race in jra_schedule.races:
                all_races[race.race_id] = race
            logger.info(f"Fetched {len(jra_schedule.races)} races from JRA")
        except Exception as e:
            logger.error(f"Failed to fetch from JRA: {e}")

        try:
            netkeiba_schedule = self.netkeiba_scraper.get_weekly_races()
            for race in netkeiba_schedule.races:
                if race.race_id in all_races:
                    existing = all_races[race.race_id]
                    all_races[race.race_id] = self._merge_race_info(existing, race)
                else:
                    all_races[race.race_id] = race
            logger.info(f"Fetched {len(netkeiba_schedule.races)} races from netkeiba")
        except Exception as e:
            logger.error(f"Failed to fetch from netkeiba: {e}")

        from backend.utils.date_utils import get_current_week_dates
        week_start, week_end = get_current_week_dates()

        return RaceSchedule(
            week_start=week_start,
            week_end=week_end,
            races=sorted(all_races.values(), key=lambda r: (r.race_date, r.venue, r.race_number)),
            fetched_at=datetime.now(),
        )

    def get_race_card(self, race_id: str) -> RaceCard | None:
        """Get race card with all entries.

        Args:
            race_id: Race ID

        Returns:
            RaceCard with entries or None
        """
        try:
            return self.netkeiba_scraper.get_race_card(race_id)
        except Exception as e:
            logger.error(f"Failed to get race card for {race_id}: {e}")
            return None

    def get_race_result(self, race_id: str) -> RaceWithResults | None:
        """Get race with results.

        Args:
            race_id: Race ID

        Returns:
            RaceWithResults or None
        """
        try:
            race_card = self.netkeiba_scraper.get_race_card(race_id)
            if not race_card:
                return None

            results = self.netkeiba_scraper.get_race_result(race_id)
            if not results:
                return None

            return RaceWithResults(race=race_card.race, results=results)
        except Exception as e:
            logger.error(f"Failed to get race result for {race_id}: {e}")
            return None

    def get_horse_info(self, horse_id: str) -> Horse | None:
        """Get detailed horse information.

        Args:
            horse_id: Horse ID

        Returns:
            Horse information or None
        """
        try:
            return self.netkeiba_scraper.get_horse_info(horse_id)
        except Exception as e:
            logger.error(f"Failed to get horse info for {horse_id}: {e}")
            return None

    def get_horses_for_race(self, race_id: str) -> list[HorseBasicInfo]:
        """Get all horses entered in a race with basic info.

        Args:
            race_id: Race ID

        Returns:
            List of horses with basic info
        """
        horses = []

        try:
            race_card = self.netkeiba_scraper.get_race_card(race_id)
            if not race_card:
                return horses

            for entry in race_card.entries:
                if entry.horse_id:
                    performances = self.netkeiba_scraper.get_horse_performances(
                        entry.horse_id, limit=5
                    )
                    horse_info = HorseBasicInfo(
                        horse_id=entry.horse_id,
                        horse_name=entry.horse_name,
                        sex=entry.sex or "不明",
                        age=entry.age or 0,
                        trainer_name=entry.trainer_name,
                        recent_performances=performances,
                    )
                    horses.append(horse_info)

        except Exception as e:
            logger.error(f"Failed to get horses for race {race_id}: {e}")

        return horses

    def get_venue_schedules(self) -> list[WeeklyVenueSchedule]:
        """Get weekly schedules for all active venues.

        Returns:
            List of venue schedules
        """
        try:
            return self.jra_scraper.get_venue_schedules()
        except Exception as e:
            logger.error(f"Failed to get venue schedules: {e}")
            return []

    def get_course_info(self, venue: str) -> Course | None:
        """Get course information for a venue.

        Args:
            venue: Venue name (e.g., "東京")

        Returns:
            Course information or None
        """
        try:
            return self.jra_scraper.get_course_info(venue)
        except Exception as e:
            logger.error(f"Failed to get course info for {venue}: {e}")
            return None

    def get_course_conditions(self, venue: str) -> CourseCondition | None:
        """Get current course conditions.

        Args:
            venue: Venue name

        Returns:
            Course conditions or None
        """
        try:
            return self.jra_scraper.get_course_conditions(venue)
        except Exception as e:
            logger.error(f"Failed to get course conditions for {venue}: {e}")
            return None

    def get_all_course_info(self) -> list[Course]:
        """Get course information for all JRA venues.

        Returns:
            List of Course objects for all venues
        """
        courses = []
        for venue in self.jra_scraper.VENUE_CODES.keys():
            course = self.get_course_info(venue)
            if course:
                courses.append(course)
        return courses

    def _merge_race_info(self, race1: Race, race2: Race) -> Race:
        """Merge race information from two sources.

        Prefers non-empty values, with race2 taking precedence for conflicts.

        Args:
            race1: First race (usually from JRA)
            race2: Second race (usually from netkeiba)

        Returns:
            Merged race
        """
        return Race(
            race_id=race1.race_id,
            race_name=(
                race2.race_name
                if len(race2.race_name) > len(race1.race_name)
                else race1.race_name
            ),
            race_number=race1.race_number,
            race_date=race1.race_date,
            post_time=race2.post_time or race1.post_time,
            venue=race1.venue,
            venue_code=race1.venue_code or race2.venue_code,
            round_number=race1.round_number or race2.round_number,
            day_number=race1.day_number or race2.day_number,
            distance=race2.distance if race2.distance > 0 else race1.distance,
            track_type=race2.track_type if race2.track_type != "不明" else race1.track_type,
            direction=race2.direction or race1.direction,
            track_condition=race2.track_condition or race1.track_condition,
            weather=race2.weather or race1.weather,
            race_class=race2.race_class or race1.race_class,
            race_grade=race2.race_grade or race1.race_grade,
            age_condition=race2.age_condition or race1.age_condition,
            sex_condition=race2.sex_condition or race1.sex_condition,
            weight_condition=race2.weight_condition or race1.weight_condition,
            prize_money=race2.prize_money or race1.prize_money,
            entry_count=race2.entry_count or race1.entry_count,
        )

    def close(self) -> None:
        """Close all scrapers and release resources."""
        if self._jra_scraper:
            self._jra_scraper.close()
            self._jra_scraper = None
        if self._netkeiba_scraper:
            self._netkeiba_scraper.close()
            self._netkeiba_scraper = None

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
        return False
