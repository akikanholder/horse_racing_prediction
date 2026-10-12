"""Tests for track information scrapers."""

import pytest
from datetime import datetime

from backend.models.track import (
    CourseInfo,
    CourseType,
    CushionValue,
    MoistureContent,
    TrackCondition,
    TrackInfo,
    Weather,
)
from backend.scrapers.jra_track_scraper import JRATrackScraper
from backend.scrapers.netkeiba_track_scraper import NetkeibaTrackScraper
from backend.services.track_info_service import TrackInfoService


class TestTrackCondition:
    """Tests for TrackCondition enum."""

    def test_from_string_good(self):
        assert TrackCondition.from_string("良") == TrackCondition.GOOD

    def test_from_string_slightly_heavy(self):
        assert TrackCondition.from_string("稍重") == TrackCondition.SLIGHTLY_HEAVY

    def test_from_string_heavy(self):
        assert TrackCondition.from_string("重") == TrackCondition.HEAVY

    def test_from_string_bad(self):
        assert TrackCondition.from_string("不良") == TrackCondition.BAD

    def test_from_string_with_whitespace(self):
        assert TrackCondition.from_string("  良  ") == TrackCondition.GOOD

    def test_from_string_unknown(self):
        assert TrackCondition.from_string("invalid") == TrackCondition.UNKNOWN

    def test_from_string_partial_slightly_heavy(self):
        assert TrackCondition.from_string("稍") == TrackCondition.SLIGHTLY_HEAVY


class TestWeather:
    """Tests for Weather enum."""

    def test_from_string_sunny(self):
        assert Weather.from_string("晴") == Weather.SUNNY

    def test_from_string_rainy(self):
        assert Weather.from_string("雨") == Weather.RAINY

    def test_from_string_cloudy(self):
        assert Weather.from_string("曇") == Weather.CLOUDY

    def test_from_string_compound_sunny_then_cloudy(self):
        assert Weather.from_string("晴後曇") == Weather.SUNNY_THEN_CLOUDY

    def test_from_string_compound_rainy_then_cloudy(self):
        assert Weather.from_string("雨後曇") == Weather.RAINY_THEN_CLOUDY

    def test_from_string_unknown(self):
        assert Weather.from_string("invalid") == Weather.UNKNOWN


class TestCushionValue:
    """Tests for CushionValue model."""

    def test_number_does_not_invent_a_grade(self):
        cushion = CushionValue(value=9.6)
        assert cushion.value == 9.6
        assert cushion.grade is None

    def test_printed_grade_is_kept(self):
        cushion = CushionValue(value=9.6, grade="標準")
        assert cushion.value == 9.6
        assert cushion.grade == "標準"

    def test_scale_threshold_is_not_stored_as_a_grade(self):
        cushion = CushionValue(value=12.5)
        assert cushion.grade is None

    def test_missing_number_stays_null(self):
        cushion = CushionValue(value=None)
        assert cushion.value is None
        assert cushion.grade is None


class TestMoistureContent:
    """Tests for MoistureContent model."""

    def test_create_with_values(self):
        moisture = MoistureContent(goal_front=13.1, fourth_corner=14.1)
        assert moisture.goal_front == 13.1
        assert moisture.fourth_corner == 14.1

    def test_create_partial(self):
        moisture = MoistureContent(goal_front=13.1)
        assert moisture.goal_front == 13.1
        assert moisture.fourth_corner is None


class TestCourseInfo:
    """Tests for CourseInfo model."""

    def test_create_turf_course(self):
        course = CourseInfo(
            course_type=CourseType.TURF,
            condition=TrackCondition.GOOD,
            moisture=MoistureContent(goal_front=10.0),
            cushion=CushionValue(value=9.5),
        )
        assert course.course_type == CourseType.TURF
        assert course.condition == TrackCondition.GOOD
        assert course.moisture.goal_front == 10.0
        assert course.cushion.value == 9.5

    def test_create_dirt_course(self):
        course = CourseInfo(
            course_type=CourseType.DIRT,
            condition=TrackCondition.HEAVY,
        )
        assert course.course_type == CourseType.DIRT
        assert course.condition == TrackCondition.HEAVY
        assert course.cushion is None


class TestTrackInfo:
    """Tests for TrackInfo model."""

    def test_create_track_info(self):
        track_info = TrackInfo(
            racecourse="中山",
            date=datetime(2026, 9, 22),
            weather=Weather.SUNNY,
            source="JRA",
        )
        assert track_info.racecourse == "中山"
        assert track_info.date == datetime(2026, 9, 22)
        assert track_info.weather == Weather.SUNNY
        assert track_info.source == "JRA"

    def test_json_serialization(self):
        track_info = TrackInfo(
            racecourse="東京",
            date=datetime(2026, 9, 22),
            source="Netkeiba",
        )
        json_data = track_info.model_dump_json()
        assert "東京" in json_data
        assert "Netkeiba" in json_data


class TestJRATrackScraper:
    """Tests for JRA scraper."""

    def test_get_base_url(self):
        scraper = JRATrackScraper()
        assert scraper.get_base_url() == "https://www.jra.go.jp"

    def test_get_track_url_default(self):
        scraper = JRATrackScraper()
        url = scraper.get_track_url()
        assert url == "https://www.jra.go.jp/keiba/baba/index.html"

    def test_get_track_url_with_racecourse(self):
        scraper = JRATrackScraper()
        url = scraper.get_track_url("nakayama")
        assert url == "https://www.jra.go.jp/keiba/baba/nakayama.html"

    def test_get_available_racecourses(self):
        scraper = JRATrackScraper()
        courses = scraper.get_available_racecourses()
        assert len(courses) == 10
        assert ("nakayama", "中山") in courses
        assert ("tokyo", "東京") in courses


class TestNetkeibaTrackScraper:
    """Tests for Netkeiba scraper."""

    def test_get_base_url(self):
        scraper = NetkeibaTrackScraper()
        assert scraper.get_base_url() == "https://race.netkeiba.com"

    def test_get_track_url_default(self):
        scraper = NetkeibaTrackScraper()
        url = scraper.get_track_url()
        assert url == "https://race.netkeiba.com/race/track.html"

    def test_get_track_url_with_racecourse(self):
        scraper = NetkeibaTrackScraper()
        url = scraper.get_track_url("06")
        assert "jyo=06" in url

    def test_get_track_url_with_date(self):
        scraper = NetkeibaTrackScraper()
        url = scraper.get_track_url("06", "20260922")
        assert "jyo=06" in url
        assert "kaisai_date=20260922" in url

    def test_get_available_racecourses(self):
        scraper = NetkeibaTrackScraper()
        courses = scraper.get_available_racecourses()
        assert len(courses) == 10
        assert ("06", "中山") in courses
        assert ("05", "東京") in courses


class TestTrackInfoService:
    """Tests for TrackInfoService."""

    def test_get_all_racecourses(self):
        service = TrackInfoService()
        courses = service.get_all_racecourses()
        assert len(courses) == 10

        nakayama = next(c for c in courses if c["name"] == "中山")
        assert nakayama["jra_code"] == "nakayama"
        assert nakayama["netkeiba_id"] == "06"

    def test_context_manager(self):
        with TrackInfoService() as service:
            courses = service.get_all_racecourses()
            assert len(courses) == 10


@pytest.mark.integration
class TestJRATrackScraperIntegration:
    """Integration tests for JRA scraper (requires network access)."""

    def test_scrape_default_page(self):
        with JRATrackScraper() as scraper:
            track_info = scraper.scrape()
            assert track_info.source == "JRA"
            assert track_info.racecourse is not None


@pytest.mark.integration
class TestNetkeibaTrackScraperIntegration:
    """Integration tests for Netkeiba scraper (requires network access)."""

    def test_scrape_default_page(self):
        with NetkeibaTrackScraper() as scraper:
            track_info = scraper.scrape()
            assert track_info.source == "Netkeiba"


@pytest.mark.integration
class TestTrackInfoServiceIntegration:
    """Integration tests for TrackInfoService (requires network access)."""

    def test_get_track_info_from_both_sources(self):
        with TrackInfoService() as service:
            results = service.get_track_info()
            assert "jra" in results or "netkeiba" in results

    def test_get_track_info_merged(self):
        with TrackInfoService() as service:
            results = service.get_track_info(merge=True)
            if len(results) > 1:
                assert "merged" in results
