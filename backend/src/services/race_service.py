"""Race service for aggregating data from multiple sources."""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Literal, cast

from backend.models.course import Course, CourseCondition, WeeklyVenueSchedule
from backend.models.historical import HistoricalBatch, HistoricalExport
from backend.models.horse import Horse, HorseBasicInfo
from backend.models.odds import (
    ODDS_ENCODING,
    BetType,
    FinalRaceOdds,
    OddsPrefer,
    OddsStatus,
    RaceOdds,
    RacePhase,
)
from backend.models.race import (
    Race,
    RaceCard,
    RaceEntry,
    RaceResult,
    RaceSchedule,
    RaceWithResults,
    SettledRaceResult,
    horse_track_bias,
    track_bias_from_race,
)
from backend.models.race_trends import RaceTrendReport
from backend.models.training import TRAINING_FETCH_FAILED, unavailable_training
from backend.scrapers.day_of_bias import DayOfBiasClient, fetch_failed_publication
from backend.scrapers.jra_headless import close_shared
from backend.scrapers.jra_scraper import JRAScraper
from backend.scrapers.netkeiba_odds_scraper import NetkeibaOddsScraper
from backend.scrapers.netkeiba_scraper import NetkeibaScraper
from backend.scrapers.runner_status import (
    JRA_ODDS_FETCH_FAILED,
    SCRATCH_STATUS_MISSING,
    ScratchPage,
    apply_scratch_page,
    join_warnings,
    merge_non_runners,
)
from backend.scrapers.training_scraper import TrainingScraper, credentials_from_env
from backend.services.final_odds_service import FinalOddsService
from backend.services.historical_service import HistoricalService
from backend.services.race_trend_service import RaceTrendService

logger = logging.getLogger(__name__)


class RaceService:
    """Service for aggregating race data from multiple sources."""

    def __init__(self, fetch_mode: str = "html"):
        """Initialize race service.

        ``fetch_mode`` is ``html`` (the HTTP path), ``headless``, or ``auto``.
        It applies to JRA official pages. netkeiba near-runs stay on HTTP.
        """
        if fetch_mode not in {"html", "headless", "auto"}:
            fetch_mode = "html"
        self.fetch_mode = fetch_mode
        self._jra_scraper: JRAScraper | None = None
        self._netkeiba_scraper: NetkeibaScraper | None = None
        self._odds_scraper: NetkeibaOddsScraper | None = None
        self._training_scraper: TrainingScraper | None = None
        self._day_of_bias: DayOfBiasClient | None = None

    @property
    def jra_scraper(self) -> JRAScraper:
        """Get or create JRA scraper."""
        if self._jra_scraper is None:
            self._jra_scraper = JRAScraper(fetch_mode=self.fetch_mode)  # type: ignore[arg-type]
        return self._jra_scraper

    @property
    def netkeiba_scraper(self) -> NetkeibaScraper:
        """Get or create Netkeiba scraper."""
        if self._netkeiba_scraper is None:
            self._netkeiba_scraper = NetkeibaScraper()
            self._netkeiba_scraper.fetch_mode = self.fetch_mode
        return self._netkeiba_scraper

    @property
    def odds_scraper(self) -> NetkeibaOddsScraper:
        """Get or create the netkeiba odds client."""
        if self._odds_scraper is None:
            self._odds_scraper = NetkeibaOddsScraper()
        return self._odds_scraper

    def get_weekly_races(self, *, fetch_mode: str | None = None) -> RaceSchedule:
        """Get this week's race schedule from all sources."""
        with self._using(fetch_mode):
            return self._weekly_races()

    def _weekly_races(self) -> RaceSchedule:
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

    def get_races_for_date(self, target_date: date) -> list[Race]:
        """Get every race held on the given date.

        Combines the weekly schedule (filtered to the date) with the
        netkeiba race list for that date.

        Args:
            target_date: Date to collect races for

        Returns:
            Races sorted by venue and race number
        """
        all_races: dict[str, Race] = {}

        try:
            schedule = self.get_weekly_races()
            for race in schedule.races:
                if race.race_date == target_date:
                    all_races[race.race_id] = race
            logger.info(
                "Found %s weekly races on %s",
                sum(1 for race in schedule.races if race.race_date == target_date),
                target_date,
            )
        except Exception as e:
            logger.error(f"Failed to filter weekly races for {target_date}: {e}")

        try:
            dated_races = self.netkeiba_scraper.get_races_by_date(target_date)
            for race in dated_races:
                if race.race_id in all_races:
                    all_races[race.race_id] = self._merge_race_info(all_races[race.race_id], race)
                else:
                    all_races[race.race_id] = race
            logger.info(f"Fetched {len(dated_races)} races for {target_date} from netkeiba")
        except Exception as e:
            logger.error(f"Failed to fetch races for {target_date}: {e}")

        return sorted(all_races.values(), key=lambda race: (race.venue, race.race_number))

    def get_race_card(self, race_id: str, *, fetch_mode: str | None = None) -> RaceCard | None:
        """Get race card with all entries."""
        with self._using(fetch_mode):
            return self._race_card(race_id)

    def _race_card(self, race_id: str) -> RaceCard | None:
        """Get race card with all entries.

        Combines the netkeiba shutuba page with the JRA accessD card.
        A source that fails is ignored when the other source returns a card.
        accessD HTTP 403 from datacenter egress is that kind of failure: the
        netkeiba card is kept. jradb may need a residential browser later.

        Args:
            race_id: Race ID

        Returns:
            RaceCard with entries or None
        """
        netkeiba_card: RaceCard | None = None
        jra_card: RaceCard | None = None

        try:
            netkeiba_card = self.netkeiba_scraper.get_race_card(race_id)
        except Exception as e:
            logger.error(f"Failed to get netkeiba race card for {race_id}: {e}")

        try:
            jra_card = self.jra_scraper.get_race_card(race_id)
        except Exception as e:
            logger.warning(
                "JRA accessD card unavailable for %s (%s); keeping the netkeiba card",
                race_id,
                e,
            )

        return self._attach_day_of_bias(self._merge_race_cards(netkeiba_card, jra_card))

    def get_race_export_bundle(self, race_id: str) -> tuple[RaceCard | None, list[RaceResult]]:
        """Load a race card and its results without fetching the card twice.

        Args:
            race_id: Race ID

        Returns:
            Race card (if available) and result rows
        """
        race_card = self.get_race_card(race_id)
        results: list[RaceResult] = []
        try:
            fetched = self.netkeiba_scraper.get_race_result(race_id)
            if fetched:
                results = fetched
        except Exception as e:
            logger.error(f"Failed to get race result for {race_id}: {e}")
        return race_card, results

    def get_race_result(self, race_id: str) -> RaceWithResults | None:
        """Get race with results.

        Args:
            race_id: Race ID

        Returns:
            RaceWithResults or None
        """
        try:
            race_card = self.get_race_card(race_id)
            if not race_card:
                return None

            results = self.netkeiba_scraper.get_race_result(race_id)
            if not results:
                return None

            return RaceWithResults(race=race_card.race, results=results)
        except Exception as e:
            logger.error(f"Failed to get race result for {race_id}: {e}")
            return None

    def get_settled_race_result(
        self,
        race_id: str,
        *,
        fetch_mode: str | None = None,
        wait_complete: bool = False,
        retry_interval: float = 60,
        max_wait: float = 600,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> SettledRaceResult:
        """Settled finish order and payouts for one race."""
        with self._using(fetch_mode):
            return self._settled_race_result(
                race_id,
                wait_complete=wait_complete,
                retry_interval=retry_interval,
                max_wait=max_wait,
                sleeper=sleeper,
                clock=clock,
                now=now,
            )

    def _settled_race_result(
        self,
        race_id: str,
        *,
        wait_complete: bool = False,
        retry_interval: float = 60,
        max_wait: float = 600,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> SettledRaceResult:
        """Settled finish order and payouts for one race.

        This is the post-race settlement used for mark × finish logs.
        It is not the horse near-run history from ``cli horse``.
        Do not feed it into pre-race EV. A miss returns empty rows and warning.

        Args:
            race_id: Race ID

        Returns:
            Settled race result from the netkeiba result page
        """
        if not wait_complete:
            return self.netkeiba_scraper.get_settled_race_result(race_id)
        kwargs: dict[str, object] = {
            "wait_complete": True,
            "retry_interval": retry_interval,
            "max_wait": max_wait,
        }
        if sleeper is not None:
            kwargs["sleeper"] = sleeper
        if clock is not None:
            kwargs["clock"] = clock
        if now is not None:
            kwargs["now"] = now
        return self.netkeiba_scraper.get_settled_race_result(race_id, **kwargs)  # type: ignore[arg-type]

    def get_historical_races(
        self,
        *,
        race_id: str | None = None,
        on_date: date | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        venues: list[str] | None = None,
        flat_only: bool = True,
        priority_venues: bool = False,
        delay_seconds: float = 1.5,
        resume_from: str | None = None,
        fetch_mode: str | None = None,
    ) -> HistoricalBatch:
        """Past flat races for an EV backtest.

        ``race_id`` loads one race. ``on_date`` or ``date_from``/``date_to``
        loads each race in order, with a delay, from 2025 onward. Final odds
        are kept only when the page says 確定オッズ or the page is post-race.
        Live odds are not copied.
        """
        mode = fetch_mode if fetch_mode in {"html", "headless", "auto"} else self.fetch_mode
        with HistoricalService(delay_seconds=delay_seconds, fetch_mode=mode) as service:
            return service.get(
                race_id=race_id,
                on_date=on_date,
                date_from=date_from,
                date_to=date_to,
                venues=venues,
                flat_only=flat_only,
                priority_venues=priority_venues,
                resume_from=resume_from,
            )

    @staticmethod
    def export_historical_races(
        batch: HistoricalBatch,
        directory: str,
        fmt: str,
    ) -> HistoricalExport:
        """Write JSONL or per-surface CSV. Out-of-scope races are left out."""
        return HistoricalService.export(batch, Path(directory), fmt)

    def get_race_trends(
        self,
        *,
        venues: list[str] | None = None,
        surface: str | None = None,
        distance: int | None = None,
        race_class: str | None = None,
        going: str | None = None,
        on_date: date | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        recent_meeting: bool = False,
        as_of: date | None = None,
        today: bool = False,
        fetch_mode: str | None = None,
    ) -> RaceTrendReport:
        """Win, top-2, top-3, and ROI tables over settled history.

        The rows come from ``history`` and the local page cache. ``today`` is
        one meeting day in Asia/Tokyo. Only a confirmed and complete result
        is counted. A missing price or sire stays null. A 403 or a login wall
        is not retried. A non-final result page is fetched again on the next run.
        """
        mode = fetch_mode if fetch_mode in {"html", "headless", "auto"} else self.fetch_mode
        with RaceTrendService(fetch_mode=mode) as service:
            return service.get(
                venues=venues,
                surface=surface,
                distance=distance,
                race_class=race_class,
                going=going,
                on_date=on_date,
                date_from=date_from,
                date_to=date_to,
                recent_meeting=recent_meeting,
                as_of=as_of,
                today=today,
            )

    def get_final_combination_odds(
        self,
        race_id: str,
        bet_types: str | list[str] | None = None,
        *,
        fetch_mode: str | None = None,
    ) -> FinalRaceOdds:
        """Final wide, umaren, and sanrenpuku odds for a past race.

        Prices stay null unless the result is confirmed and complete.
        ``html`` does not open a browser. ``headless`` and ``auto`` may.
        A 403 or a login wall stops the page chain.
        """
        chosen = fetch_mode if fetch_mode in {"html", "headless", "auto"} else self.fetch_mode
        mode = cast(Literal["html", "headless", "auto"], chosen)
        headless: Literal["auto", "off"] = "off" if mode == "html" else "auto"
        with FinalOddsService(fetch_mode=mode, headless_render=headless) as service:
            return service.get(race_id, bet_types)

    def get_race_odds(
        self,
        race_id: str,
        bet_types: str | list[str] | None = "win",
        prefer: str = "live",
        *,
        fetch_mode: str | None = None,
    ) -> RaceOdds:
        """Get win, wide, umaren, and sanrenpuku odds quotes for EV."""
        with self._using(fetch_mode):
            return self._race_odds(race_id, bet_types, prefer)

    def _race_odds(
        self,
        race_id: str,
        bet_types: str | list[str] | None = "win",
        prefer: str = "live",
    ) -> RaceOdds:
        """Get win, wide, umaren, and sanrenpuku odds quotes for EV.

        Quotes stay on this object. RaceEntry.odds is not updated and is not
        the source of truth. prefer does not rewrite kind: a yoso payload is
        returned as kind=yoso even when the caller prefers live.

        prefer=live does not fall back to yoso when the market is unavailable.
        Callers that want predicted odds pass prefer=yoso themselves.
        encoding is utf-8. race_phase is pre or post when the API status says so.
        A miss returns quotes=[] and reason. Prices are not invented.

        Args:
            race_id: Race ID
            bet_types: Requested bet types. win, wide, umaren, and sanrenpuku are fetched.
            prefer: live or yoso. Stored on the snapshot, not used to relabel.

        Returns:
            RaceOdds. quotes is empty when the market has no numbers.
        """
        fetched_at = datetime.now()
        requested = _normalize_bet_types(bet_types)
        preference: OddsPrefer = "yoso" if prefer == "yoso" else "live"
        selected = _supported_bet_types(requested)
        if not selected:
            return RaceOdds(
                race_id=race_id,
                bet_types=[],
                prefer=preference,
                status="unavailable",
                reason="win odds were not requested",
                encoding=ODDS_ENCODING,
                race_phase=None,
                quotes=[],
                fetched_at=fetched_at,
            )

        snapshots = [
            self._fetch_bet_odds(race_id, bet_type, fetched_at, preference) for bet_type in selected
        ]
        if len(snapshots) == 1:
            snapshot = snapshots[0].model_copy(update={"prefer": preference, "bet_types": selected})
        else:
            snapshot = _merge_odds_snapshots(race_id, selected, preference, fetched_at, snapshots)
        if isinstance(self._odds_scraper, NetkeibaOddsScraper):
            snapshot = self._prefer_jra_scratches(snapshot, race_id)
        return snapshot

    def _fetch_bet_odds(
        self,
        race_id: str,
        bet_type: BetType,
        fetched_at: datetime,
        preference: OddsPrefer,
    ) -> RaceOdds:
        """Fetch one bet type. Does not retry with the other kind."""
        if bet_type == "win":
            fetch = self.odds_scraper.get_win_odds
        elif bet_type == "wide":
            fetch = self.odds_scraper.get_wide_odds
        elif bet_type == "umaren":
            fetch = self.odds_scraper.get_umaren_odds
        else:
            fetch = self.odds_scraper.get_sanrenpuku_odds
        try:
            snapshot = fetch(race_id, fetched_at=fetched_at)
        except Exception as e:
            logger.error("Failed to get %s odds for %s: %s", bet_type, race_id, e)
            return RaceOdds(
                race_id=race_id,
                bet_types=[bet_type],
                prefer=preference,
                status="unavailable",
                reason="fetch failed",
                encoding=ODDS_ENCODING,
                race_phase=None,
                quotes=[],
                fetched_at=fetched_at,
            )
        return snapshot.model_copy(update={"prefer": preference, "bet_types": [bet_type]})

    def _prefer_jra_scratches(self, snapshot: RaceOdds, race_id: str) -> RaceOdds:
        """JRA scratch marks win. A 403 keeps the netkeiba list and warns.

        Odds numbers already on the snapshot are not replaced with a JRA price.
        A JRA ``----`` or ``取消`` nulls that quote.
        """
        try:
            page = self.jra_scraper.odds_scratches(race_id, fetched_at=snapshot.fetched_at)
        except Exception as e:
            logger.warning("JRA odds scratch lookup failed for %s: %s", race_id, e)
            page = ScratchPage(None, set(), JRA_ODDS_FETCH_FAILED, False)
        return apply_scratch_page(snapshot, page)

    @property
    def training_scraper(self) -> TrainingScraper:
        """Get or create the training client (競馬ブック and speedkeiba).

        Keibabook login is attempted only when ``KEIBABOOK_LOGIN_ID`` and
        ``KEIBABOOK_PASSWORD`` are set, or when ``KEIBABOOK_COOKIE`` is set.
        """
        if self._training_scraper is None:
            self._training_scraper = TrainingScraper(credentials=credentials_from_env())
        return self._training_scraper

    def get_horse_info(self, horse_id: str) -> Horse | None:
        """Get horse identity and near-run history.

        ``performances`` are past runs from the horse result page. They are not
        the result of a race being predicted. ``training`` is this week's
        speedkeiba article when the horse name is on it. When keibabook login
        succeeds, ``/db/uma/{umacd}/crireki`` is used if a search row or profile
        page links that umacd and prints the same birth date. A miss stays null.

        Args:
            horse_id: Horse ID

        Returns:
            Horse information or None
        """
        try:
            horse = self.netkeiba_scraper.get_horse_info(horse_id)
        except Exception as e:
            logger.error(f"Failed to get horse info for {horse_id}: {e}")
            return None
        if horse is None:
            return None
        try:
            training = self.training_scraper.training_for_horse(
                horse.horse_name,
                birth_date=horse.birth_date,
                sex=horse.sex,
            )
        except Exception as e:
            logger.error("Failed to get training for %s: %s", horse_id, e)
            training = unavailable_training(TRAINING_FETCH_FAILED)
        return horse.model_copy(update={"training": training})

    def get_horses_for_race(self, race_id: str) -> list[HorseBasicInfo]:
        """Get entrants with near-run history, pedigree, and running style.

        ``recent_performances`` is that horse's public past runs, at most five,
        not this race's result. Fewer than five public runs set ``warning``.
        ``login_required_for_more`` is taken from the result page. No netkeiba
        login is attempted.
        Pedigree comes from ``/horse/ped/{horse_id}/``. Running style is derived
        only from those near runs (at most five).         ``track_bias`` copies the
        card's venue, surface, distance, and going. Inside/outside and pace
        bias are copied only when accessD or JRA baba prints those labels.
        ``training`` is the keibabook workout table for this
        race when that horse is on the page. A logged-in session also reads
        each missing horse's crireki, using the umacd on the shutuba name link.
        Otherwise the speedkeiba article whose title is this race.
        Clocks that are not on those pages stay null.

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

            race_card = self._attach_day_of_bias(race_card)
            if race_card is None:
                return horses
            bias = race_card.race.track_bias or horse_track_bias()
            names = [
                entry.horse_name
                for entry in race_card.entries
                if entry.horse_id and entry.horse_name
            ]
            try:
                training_by_name = self.training_scraper.training_for_race(
                    race_id, race_card.race.race_name, names
                )
            except Exception as e:
                logger.error("Failed to get training for race %s: %s", race_id, e)
                training_by_name = {
                    name: unavailable_training(TRAINING_FETCH_FAILED) for name in names
                }
            for entry in race_card.entries:
                if entry.horse_id:
                    fetched = self.netkeiba_scraper.get_horse_performances(entry.horse_id, limit=5)
                    (
                        performances,
                        runs_fetched,
                        login_required_for_more,
                        run_source,
                        run_fetched_at,
                        run_warning,
                    ) = _recent_runs_from_fetch(fetched)
                    pedigree = self.netkeiba_scraper.get_pedigree(entry.horse_id)
                    training = training_by_name.get(entry.horse_name) or unavailable_training(
                        TRAINING_FETCH_FAILED
                    )
                    horse_info = HorseBasicInfo(
                        horse_id=entry.horse_id,
                        horse_name=entry.horse_name,
                        sex=entry.sex or "不明",
                        age=entry.age or 0,
                        sire_name=pedigree.sire.name if pedigree.sire else None,
                        dam_name=pedigree.dam.name if pedigree.dam else None,
                        trainer_name=entry.trainer_name,
                        recent_performances=performances,
                        runs_fetched=runs_fetched,
                        login_required_for_more=login_required_for_more,
                        source=run_source,
                        fetched_at=run_fetched_at,
                        warning=run_warning,
                        pedigree=pedigree,
                        track_bias=bias,
                        training=training,
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

    def _merge_race_cards(
        self, netkeiba_card: RaceCard | None, jra_card: RaceCard | None
    ) -> RaceCard | None:
        """Merge shutuba cards, keeping netkeiba ids when both sources have them.

        Args:
            netkeiba_card: Card from netkeiba
            jra_card: Card from JRA accessD

        Returns:
            Merged card, the only available card, or None
        """
        if netkeiba_card is None and jra_card is None:
            return None
        if netkeiba_card is None:
            return jra_card
        if jra_card is None:
            return netkeiba_card

        race = self._merge_race_info(jra_card.race, netkeiba_card.race)
        entries = self._merge_entries(netkeiba_card.entries, jra_card.entries)
        if race.entry_count is None and entries:
            race = race.model_copy(update={"entry_count": len(entries)})
        warning = None
        if entries and any(entry.status is None for entry in entries):
            warning = join_warnings(
                jra_card.warning,
                netkeiba_card.warning,
                SCRATCH_STATUS_MISSING,
            )
        return RaceCard(race=race, entries=entries, warning=warning)

    @property
    def day_of_bias(self) -> DayOfBiasClient:
        """Official pre-race bias client. Shares the accessD cache with JRA."""
        if self._day_of_bias is None:
            self._day_of_bias = DayOfBiasClient(self.jra_scraper)
        return self._day_of_bias

    def _attach_day_of_bias(self, card: RaceCard | None) -> RaceCard | None:
        """Fill inside/outside and pace from official pages. Misses stay null."""
        if card is None:
            return None
        try:
            publication = self.day_of_bias.publication_for(card.race)
        except Exception as e:
            logger.warning("Day-of track bias lookup failed for %s: %s", card.race.race_id, e)
            publication = fetch_failed_publication()
        current = card.race.track_bias or track_bias_from_race(card.race)
        bias = current.model_copy(
            update={
                "inside_outside": publication.inside_outside,
                "pace_bias": publication.pace_bias,
                "source": publication.source,
                "warning": publication.warning,
                "data_sufficiency": publication.data_sufficiency,
            }
        )
        race = card.race.model_copy(update={"track_bias": bias})
        return card.model_copy(update={"race": race})

    def _merge_entries(
        self, primary: list[RaceEntry], secondary: list[RaceEntry]
    ) -> list[RaceEntry]:
        """Fill blank starter fields from the secondary card, keyed by horse number.

        Identifier fields already present on the primary (netkeiba) entry are kept.
        """
        secondary_by_number = {entry.horse_number: entry for entry in secondary}
        merged: list[RaceEntry] = []
        seen: set[int] = set()

        for entry in primary:
            seen.add(entry.horse_number)
            other = secondary_by_number.get(entry.horse_number)
            merged.append(self._fill_entry(entry, other) if other else entry)

        for entry in secondary:
            if entry.horse_number not in seen:
                merged.append(entry)

        return sorted(merged, key=lambda entry: entry.horse_number)

    def _fill_entry(self, primary: RaceEntry, secondary: RaceEntry) -> RaceEntry:
        """Copy empty primary fields from the secondary entry."""
        updates: dict[str, object] = {}
        id_fields = {"horse_id", "jockey_id", "trainer_id"}
        status_fields = {"status", "status_label", "source", "fetched_at"}
        for field_name in RaceEntry.model_fields:
            if field_name in {"bracket_number", "horse_number"} or field_name in status_fields:
                continue
            current = getattr(primary, field_name)
            incoming = getattr(secondary, field_name)
            if field_name in id_fields:
                if not current and incoming:
                    updates[field_name] = incoming
                continue
            if current is None and incoming is not None:
                updates[field_name] = incoming
            elif current == "" and incoming:
                updates[field_name] = incoming
        if secondary.status is not None:
            updates["status"] = secondary.status
            updates["status_label"] = secondary.status_label
            updates["source"] = secondary.source
            updates["fetched_at"] = secondary.fetched_at
        return primary.model_copy(update=updates)

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
                race2.race_name if len(race2.race_name) > len(race1.race_name) else race1.race_name
            ),
            race_number=race1.race_number,
            race_date=race1.race_date,
            post_time=race2.post_time or race1.post_time,
            post_time_source=(
                race2.post_time_source if race2.post_time else race1.post_time_source
            ),
            post_time_fetched_at=(
                race2.post_time_fetched_at if race2.post_time else race1.post_time_fetched_at
            ),
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
        if self._odds_scraper:
            self._odds_scraper.close()
            self._odds_scraper = None
        if self._training_scraper:
            self._training_scraper.close()
            self._training_scraper = None
        close_shared()

    @contextmanager
    def _using(self, fetch_mode: str | None) -> Iterator[None]:
        """Apply one JRA fetch mode for the duration of a call."""
        chosen = fetch_mode if fetch_mode in {"html", "headless", "auto"} else self.fetch_mode
        previous = self.fetch_mode
        previous_jra = getattr(self._jra_scraper, "fetch_mode", None)
        previous_nk = getattr(self._netkeiba_scraper, "fetch_mode", None)
        self.fetch_mode = chosen
        if self._jra_scraper is not None and hasattr(self._jra_scraper, "fetch_mode"):
            self._jra_scraper.fetch_mode = chosen  # type: ignore[assignment]
        if self._netkeiba_scraper is not None and hasattr(self._netkeiba_scraper, "fetch_mode"):
            self._netkeiba_scraper.fetch_mode = chosen
        try:
            yield
        finally:
            self.fetch_mode = previous
            if previous_jra is not None and hasattr(self._jra_scraper, "fetch_mode"):
                self._jra_scraper.fetch_mode = previous_jra  # type: ignore[assignment]
            if previous_nk is not None and hasattr(self._netkeiba_scraper, "fetch_mode"):
                self._netkeiba_scraper.fetch_mode = previous_nk

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
        return False


def _recent_runs_from_fetch(
    fetched: object,
) -> tuple[list, int, bool | None, str | None, datetime | None, str | None]:
    """Read a public-run payload. A bare list has no login or source metadata."""
    if isinstance(fetched, list):
        return fetched, len(fetched), None, None, None, None
    performances = getattr(fetched, "performances", None)
    if not isinstance(performances, list):
        return [], 0, None, None, None, None
    runs_fetched = getattr(fetched, "runs_fetched", None)
    count = runs_fetched if isinstance(runs_fetched, int) else len(performances)
    login = getattr(fetched, "login_required_for_more", None)
    source = getattr(fetched, "source", None)
    fetched_at = getattr(fetched, "fetched_at", None)
    warning = getattr(fetched, "warning", None)
    return (
        performances,
        count,
        login if isinstance(login, bool) or login is None else None,
        source if isinstance(source, str) or source is None else None,
        fetched_at if isinstance(fetched_at, datetime) else None,
        warning if isinstance(warning, str) or warning is None else None,
    )


def _supported_bet_types(requested: list[str]) -> list[BetType]:
    """Keep win, wide, umaren, and sanrenpuku in the caller's order."""
    selected: list[BetType] = []
    for name in requested:
        if name == "win" or name == "wide" or name == "umaren" or name == "sanrenpuku":
            selected.append(name)
    return selected


def _merge_odds_snapshots(
    race_id: str,
    bet_types: list[BetType],
    preference: OddsPrefer,
    fetched_at: datetime,
    snapshots: list[RaceOdds],
) -> RaceOdds:
    """Combine bet types. An empty type adds no prices and does not relabel the rest."""
    quotes = [quote for snapshot in snapshots for quote in snapshot.quotes]
    race_phase = _shared_race_phase(snapshots)
    non_runners = merge_non_runners([snapshot.non_runners for snapshot in snapshots])
    warning = join_warnings(*(snapshot.warning for snapshot in snapshots))
    if not quotes:
        return RaceOdds(
            race_id=race_id,
            bet_types=bet_types,
            prefer=preference,
            status="unavailable",
            reason=_combined_reason(bet_types, snapshots),
            encoding=ODDS_ENCODING,
            race_phase=race_phase,
            quotes=[],
            non_runners=non_runners,
            warning=warning,
            fetched_at=fetched_at,
        )
    status: OddsStatus = "yoso" if all(quote.kind == "yoso" for quote in quotes) else "live"
    return RaceOdds(
        race_id=race_id,
        bet_types=bet_types,
        prefer=preference,
        status=status,
        reason=None,
        encoding=ODDS_ENCODING,
        race_phase=race_phase,
        quotes=quotes,
        non_runners=non_runners,
        warning=warning,
        fetched_at=fetched_at,
    )


def _shared_race_phase(snapshots: list[RaceOdds]) -> RacePhase | None:
    """Use the phase when every known snapshot agrees. Do not pick a side."""
    phases = {snapshot.race_phase for snapshot in snapshots if snapshot.race_phase is not None}
    if len(phases) == 1:
        return next(iter(phases))
    return None


def _combined_reason(bet_types: list[BetType], snapshots: list[RaceOdds]) -> str:
    """Join empty-market reasons. A single snapshot keeps the API reason."""
    if len(snapshots) == 1:
        return snapshots[0].reason or "odds unavailable"
    parts: list[str] = []
    for bet_type, snapshot in zip(bet_types, snapshots, strict=True):
        if snapshot.quotes:
            continue
        parts.append(f"{bet_type}: {snapshot.reason or 'odds unavailable'}")
    return "; ".join(parts) or "odds unavailable"


def _normalize_bet_types(bet_types: str | list[str] | None) -> list[str]:
    """Split comma-separated bet types. Default is win."""
    if bet_types is None:
        return ["win"]
    raw_items = [bet_types] if isinstance(bet_types, str) else list(bet_types)
    names: list[str] = []
    for item in raw_items:
        for part in str(item).split(","):
            name = part.strip().lower()
            if name and name not in names:
                names.append(name)
    return names or ["win"]
