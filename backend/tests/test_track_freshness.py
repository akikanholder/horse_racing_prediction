"""Race-day baba freshness: printed clock, stale warning, and an uncached live URL."""

from datetime import date, datetime
from pathlib import Path

import pytest
import requests

from backend.models.track import TrackInfo
from backend.scrapers.jra_track_scraper import JRATrackScraper
from backend.scrapers.page_cache import CachingHTTPClient
from backend.scrapers.track_freshness import (
    CACHED_COPY,
    RACE_DATE_UNREADABLE,
    STALE_PREFIX,
    cached_copy_warning,
    needs_fresh_wait,
    page_measurement_time,
    track_blocked,
)
from backend.services.track_info_service import TrackInfoService
from backend.utils.date_utils import TOKYO

FIXTURES = Path(__file__).parent / "fixtures"
BABA_TOKYO = "https://www.jra.go.jp/keiba/baba/index.html"
BABA_KYOTO = "https://www.jra.go.jp/keiba/baba/index2.html"
PINNED = datetime(2026, 10, 18, 15, 30, tzinfo=TOKYO)
RACE_DAY = date(2026, 10, 18)


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    error = requests.HTTPError(f"http {status}")
    error.response = response
    return error


def _baba(venue: str, body: str = "") -> str:
    return (
        "<html><head><title>馬場情報（"
        f"{venue}競馬場）　JRA</title></head><body>{body}</body></html>"
    )


def _fresh_tokyo() -> str:
    return _baba(
        "東京",
        """
        <p>2026年10月18日（日曜） 東京競馬場</p>
        <div class="content" id="cushion_num"><p><strong>9.9</strong></p></div>
        <p>クッション値 10月18日（日曜）15時00分現在</p>
        <table id="turf_line">
          <tr><th></th><th>ゴール前</th><th>4コーナー</th></tr>
          <tr><td>芝</td><td>15.0</td><td>14.0</td></tr>
          <tr><td>ダート</td><td>8.1</td><td>7.2</td></tr>
        </table>
        <p>含水率 10月18日（日曜）15時00分</p>
        """,
    )


class _Gets:
    def __init__(self, gets: dict | None = None, errors: dict | None = None):
        self.gets = gets or {}
        self.errors = errors or {}
        self.urls: list[str] = []
        self.headers: list[dict | None] = []
        self.served_cached_copy = False

    def get(self, url, params=None, headers=None, encoding=None):
        self.urls.append(url)
        self.headers.append(headers)
        if url in self.errors:
            raise self.errors[url]
        return self.gets.get(url, "")


def _scrape(client: _Gets, **kwargs):
    return JRATrackScraper(http_client=client, headless_render="off").scrape(  # type: ignore[arg-type]
        "tokyo",
        fetched_at=PINNED,
        race_date=RACE_DAY,
        today=RACE_DAY,
        **kwargs,
    )


def test_stale_baba_keeps_the_printed_clock_and_the_reading():
    html = (FIXTURES / "jra_baba_stale_20261016.html").read_text(encoding="utf-8")
    client = _Gets(gets={BABA_TOKYO: html, BABA_KYOTO: _baba("京都")})
    info = _scrape(client)
    assert info.racecourse == "東京"
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.value == 9.2
    assert info.turf.cushion.source == "jra_html"
    assert info.turf.cushion.fetched_at == PINNED
    assert info.measured_at == datetime(2026, 10, 16, 12, 0, tzinfo=TOKYO)
    assert info.is_today is False
    assert info.stale is True
    assert info.warning == f"{STALE_PREFIX}: 2026-10-16"
    assert client.headers[0]["Cache-Control"] == "no-cache"
    assert client.headers[0]["Pragma"] == "no-cache"


def test_same_day_measurement_is_not_stale():
    client = _Gets(gets={BABA_TOKYO: _fresh_tokyo(), BABA_KYOTO: _baba("京都")})
    info = _scrape(client)
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.value == 9.9
    assert info.measured_at == datetime(2026, 10, 18, 15, 0, tzinfo=TOKYO)
    assert info.is_today is True
    assert info.stale is False
    assert info.warning is None


def test_page_without_a_year_qualified_clock_does_not_invent_one():
    client = _Gets(
        gets={
            BABA_TOKYO: _baba("東京", "<p>クッション値 9.8</p><p>10月16日（金曜）12時00分現在</p>"),
            BABA_KYOTO: _baba("京都"),
        }
    )
    info = _scrape(client)
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.value == 9.8
    assert info.measured_at is None
    assert info.is_today is None
    assert info.stale is None
    assert info.warning == JRATrackScraper.CUSHION_UNDATED


def test_cached_copy_header_is_a_warning_and_the_number_stays():
    html = (FIXTURES / "jra_baba_stale_20261016.html").read_text(encoding="utf-8")

    class _Hit(_Gets):
        def get(self, url, params=None, headers=None, encoding=None):
            body = super().get(url, params=params, headers=headers, encoding=encoding)
            if url == BABA_TOKYO:
                self.served_cached_copy = True
            return body

    info = _scrape(_Hit(gets={BABA_TOKYO: html, BABA_KYOTO: _baba("京都")}))
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.value == 9.2
    assert info.warning is not None
    assert CACHED_COPY in info.warning
    assert STALE_PREFIX in info.warning
    assert cached_copy_warning({"Age": "120"}) is None
    assert cached_copy_warning({"x-cache": "TCP_HIT from example"}) == CACHED_COPY


def test_live_baba_bypasses_the_page_cache_and_a_past_track_page_does_not(tmp_path: Path):
    calls: list[str] = []

    class _Transport:
        def get(self, url, params=None, headers=None, encoding=None):
            calls.append(url)
            return "<html>baba</html>"

    client = CachingHTTPClient(tmp_path, transport=_Transport(), min_request_interval=0)
    baba = "https://www.jra.go.jp/keiba/baba/index.html"
    client.get(baba)
    client.get(baba)
    past = "https://race.netkeiba.com/race/track.html?kaisai_date=20200101"
    client.get(past)
    client.get(past)
    live = "https://race.netkeiba.com/race/track.html"
    client.get(live)
    client.get(live)
    assert calls == [baba, baba, past, live, live]


def test_unreadable_race_date_does_not_invent_the_comparison_day():
    html = (FIXTURES / "jra_baba_stale_20261016.html").read_text(encoding="utf-8")
    client = _Gets(gets={BABA_TOKYO: html, BABA_KYOTO: _baba("京都")})
    scraper = JRATrackScraper(http_client=client, headless_render="off")  # type: ignore[arg-type]
    service = TrackInfoService(jra_scraper=scraper)
    results = service.get_track_info(
        racecourse="東京",
        date="not-a-date",
        sources=["jra"],
        today=RACE_DAY,
    )
    info = results["jra"]
    assert info.stale is None
    assert info.measured_at == datetime(2026, 10, 16, 12, 0, tzinfo=TOKYO)
    assert info.warning == RACE_DATE_UNREADABLE


def test_wait_fresh_refetches_a_stale_reading_and_stops_on_403():
    stale = (FIXTURES / "jra_baba_stale_20261016.html").read_text(encoding="utf-8")
    fresh = _fresh_tokyo()

    class _Flip:
        def __init__(self):
            self.urls: list[str] = []

        def get(self, url, params=None, headers=None, encoding=None):
            self.urls.append(url)
            if url == BABA_TOKYO:
                return stale if self.urls.count(url) == 1 else fresh
            return _baba("京都")

    flip = _Flip()
    scraper = JRATrackScraper(http_client=flip, headless_render="off")  # type: ignore[arg-type]
    service = TrackInfoService(jra_scraper=scraper)
    slept: list[float] = []
    ticks = iter((0.0, 0.0))
    results = service.get_track_info(
        racecourse="東京",
        date="20261018",
        sources=["jra"],
        wait_fresh=True,
        retry_interval=5,
        max_wait=600,
        sleeper=slept.append,
        clock=lambda: next(ticks),
        today=RACE_DAY,
    )
    info = results["jra"]
    assert slept == [60]
    assert info.stale is False
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.value == 9.9
    assert info.measured_at == datetime(2026, 10, 18, 15, 0, tzinfo=TOKYO)
    assert flip.urls.count(BABA_TOKYO) == 2

    blocked = _Gets(errors={BABA_TOKYO: _http_error(403), BABA_KYOTO: _http_error(403)})
    denied = JRATrackScraper(http_client=blocked, headless_render="off")  # type: ignore[arg-type]
    service = TrackInfoService(jra_scraper=denied)
    slept.clear()
    results = service.get_track_info(
        racecourse="東京",
        date="20261018",
        sources=["jra"],
        wait_fresh=True,
        sleeper=slept.append,
        clock=lambda: 0.0,
        today=RACE_DAY,
    )
    assert slept == []
    assert results["jra"].warning == JRATrackScraper.BABA_403
    assert results["jra"].http_status == 403
    assert results["jra"].login_required is False
    assert blocked.urls == [BABA_TOKYO, BABA_KYOTO]


def test_login_wall_does_not_open_the_next_tab_or_wait():
    login = (
        "<html><head><title>ログイン</title></head><body>"
        "<form><input type='password' name='pswd'></form></body></html>"
    )
    client = _Gets(gets={BABA_TOKYO: login, BABA_KYOTO: _baba("京都", "<p>クッション値 9.1</p>")})
    scraper = JRATrackScraper(http_client=client, headless_render="off")  # type: ignore[arg-type]
    service = TrackInfoService(jra_scraper=scraper)
    slept: list[float] = []
    results = service.get_track_info(
        racecourse="東京",
        sources=["jra"],
        wait_fresh=True,
        sleeper=slept.append,
        clock=lambda: 0.0,
        today=RACE_DAY,
    )
    assert slept == []
    assert results["jra"].warning == JRATrackScraper.BABA_LOGIN
    assert results["jra"].http_status == 401
    assert results["jra"].login_required is True
    assert results["jra"].turf is None or results["jra"].turf.cushion is None
    assert client.urls == [BABA_TOKYO]


def _reading(**kwargs: object) -> TrackInfo:
    payload = {
        "source": "JRA",
        "measured_at": datetime(2026, 10, 16, 12, 0, tzinfo=TOKYO),
        "stale": True,
        "warning": "note 1403円",
    }
    payload.update(kwargs)
    return TrackInfo(**payload)  # type: ignore[arg-type]


def test_wait_fresh_only_when_today_and_the_measurement_is_earlier():
    earlier = _reading()
    assert track_blocked(earlier) is False
    assert needs_fresh_wait({"jra": earlier}, race_date=RACE_DAY, today=RACE_DAY) is True
    assert needs_fresh_wait({"jra": earlier}, race_date=None, today=RACE_DAY) is False
    assert needs_fresh_wait({"jra": earlier}, race_date=date(2026, 10, 17), today=RACE_DAY) is False
    assert needs_fresh_wait({"jra": earlier}, race_date=date(2026, 10, 19), today=RACE_DAY) is False
    later = _reading(measured_at=datetime(2026, 10, 19, 12, 0, tzinfo=TOKYO))
    assert needs_fresh_wait({"jra": later}, race_date=RACE_DAY, today=RACE_DAY) is False
    blocked = _reading(http_status=403)
    assert track_blocked(blocked) is True
    assert needs_fresh_wait({"jra": blocked}, race_date=RACE_DAY, today=RACE_DAY) is False
    missing = _reading(measured_at=None, stale=None)
    assert needs_fresh_wait({"jra": missing}, race_date=RACE_DAY, today=RACE_DAY) is False


def _stale_service() -> tuple[TrackInfoService, _Gets]:
    stale = (FIXTURES / "jra_baba_stale_20261016.html").read_text(encoding="utf-8")
    client = _Gets(gets={BABA_TOKYO: stale, BABA_KYOTO: _baba("京都")})
    scraper = JRATrackScraper(http_client=client, headless_render="off")  # type: ignore[arg-type]
    return TrackInfoService(jra_scraper=scraper), client


def test_wait_fresh_returns_at_once_for_an_omitted_past_or_future_date():
    for race_date in (None, "20261017", "20261019"):
        service, client = _stale_service()
        slept: list[float] = []
        results = service.get_track_info(
            racecourse="東京",
            date=race_date,
            sources=["jra"],
            wait_fresh=True,
            sleeper=slept.append,
            clock=lambda: 0.0,
            today=RACE_DAY,
            max_wait=600,
        )
        assert slept == [], race_date
        assert client.urls.count(BABA_TOKYO) == 1
        assert results["jra"].stale is True
        assert results["jra"].warning == f"{STALE_PREFIX}: 2026-10-16"


def test_wait_fresh_does_not_wait_for_a_later_measurement():
    client = _Gets(gets={BABA_TOKYO: _fresh_tokyo(), BABA_KYOTO: _baba("京都")})
    scraper = JRATrackScraper(http_client=client, headless_render="off")  # type: ignore[arg-type]
    service = TrackInfoService(jra_scraper=scraper)
    slept: list[float] = []
    results = service.get_track_info(
        racecourse="東京",
        date="20261016",
        sources=["jra"],
        wait_fresh=True,
        sleeper=slept.append,
        clock=lambda: 0.0,
        today=date(2026, 10, 16),
    )
    assert slept == []
    assert results["jra"].stale is True
    assert results["jra"].measured_at == datetime(2026, 10, 18, 15, 0, tzinfo=TOKYO)


def test_previous_line_announcement_does_not_hide_the_measurement():
    stamp = page_measurement_time("次回発表予定\n10月18日 7時00分 測定", 2026)
    assert stamp == datetime(2026, 10, 18, 7, 0, tzinfo=TOKYO)
    assert page_measurement_time("10月18日 7時00分\n発表予定", 2026) == stamp
    assert page_measurement_time("10月18日 7時00分発表予定", 2026) is None

    html = _baba(
        "東京",
        """
        <p>2026年10月18日（日曜） 東京競馬場</p>
        <h2>馬場状態</h2>
        <p>次回発表予定</p>
        <div class="content" id="cushion_num"><p><strong>9.1</strong></p></div>
        <p>10月18日 7時00分 測定</p>
        """,
    )
    info = _scrape(_Gets(gets={BABA_TOKYO: html, BABA_KYOTO: _baba("京都")}))
    assert info.measured_at == datetime(2026, 10, 18, 7, 0, tzinfo=TOKYO)
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.value == 9.1
    assert info.turf.cushion.source == "jra_html"
    assert info.turf.cushion.fetched_at == PINNED


def test_hour_without_minutes_and_noon_are_measurements():
    eight = datetime(2026, 10, 18, 8, 0, tzinfo=TOKYO)
    noon = datetime(2026, 10, 18, 12, 0, tzinfo=TOKYO)
    half = datetime(2026, 10, 18, 8, 30, tzinfo=TOKYO)
    assert page_measurement_time("10月18日 8時現在", 2026) == eight
    assert page_measurement_time("10月18日 8時半現在", 2026) == half
    assert page_measurement_time("10月18日 正午現在", 2026) == noon
    assert page_measurement_time("10月18日 15時40分 ブラジルC 発走", 2026) is None
    assert page_measurement_time("発表予定 10月18日 8時半", 2026) is None
    assert page_measurement_time("10月18日 15:30", 2026) == datetime(
        2026, 10, 18, 15, 30, tzinfo=TOKYO
    )
    assert page_measurement_time("2026年10月18日 8時現在", None) == eight
    assert page_measurement_time("2026年10月18日 正午現在", None) == noon
    assert page_measurement_time("10月18日 8時現在", None) is None
    assert page_measurement_time("発表予定 10月18日 8時現在", 2026) is None
    assert page_measurement_time("発表予定 10月18日 正午現在", 2026) is None
    assert page_measurement_time("10月18日 8時発走", 2026) is None
    assert page_measurement_time("10月18日 正午発走", 2026) is None
    assert page_measurement_time("次回発表予定\n10月18日 8時現在", 2026) == eight
    assert page_measurement_time("次回発表予定\n10月18日 正午現在", 2026) == noon
    both = "10月18日 8時現在\n発表予定 10月18日 正午現在"
    assert page_measurement_time(both, 2026) == eight

    html = _baba(
        "東京",
        """
        <p>2026年10月18日（日曜） 東京競馬場</p>
        <div class="content" id="cushion_num"><p><strong>9.0</strong></p></div>
        <p>クッション値 10月18日（日曜）8時現在</p>
        <p>含水率 10月18日（日曜）正午現在</p>
        """,
    )
    info = _scrape(_Gets(gets={BABA_TOKYO: html, BABA_KYOTO: _baba("京都")}))
    assert info.measured_at == noon
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.value == 9.0
    assert info.turf.cushion.measured_at == eight
    assert info.turf.cushion.source == "jra_html"
    assert info.turf.cushion.fetched_at == PINNED


def test_weekly_info_clock_does_not_make_a_yesterday_baba_fresh():
    html = (FIXTURES / "jra_baba_weekly_clock_20261018.html").read_text(encoding="utf-8")
    info = _scrape(_Gets(gets={BABA_TOKYO: html, BABA_KYOTO: _baba("京都")}))
    yesterday = datetime(2026, 10, 17, 7, 0, tzinfo=TOKYO)
    assert info.measured_at == yesterday
    assert info.is_today is False
    assert info.stale is True
    assert info.warning == f"{STALE_PREFIX}: 2026-10-17"
    assert needs_fresh_wait({"jra": info}, race_date=RACE_DAY, today=RACE_DAY) is True
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.value == 9.2
    assert info.turf.cushion.measured_at == yesterday
    assert info.turf.cushion.source == "jra_html"
    assert info.turf.cushion.fetched_at == PINNED
    assert info.turf.moisture is not None
    assert info.turf.moisture.measured_at == yesterday


def test_announcement_time_is_not_a_measurement():
    html = _baba(
        "東京",
        """
        <p>2026年10月18日（日曜） 東京競馬場</p>
        <div class="content" id="cushion_num"><p><strong>9.4</strong></p></div>
        <p>クッション値 10月18日（日曜）9時00分現在</p>
        <p>馬場発表予定 10月18日（日曜）16時30分</p>
        """,
    )
    info = _scrape(_Gets(gets={BABA_TOKYO: html, BABA_KYOTO: _baba("京都")}))
    assert info.measured_at == datetime(2026, 10, 18, 9, 0, tzinfo=TOKYO)

    only_announcement = _baba(
        "東京",
        "<p>2026年10月18日</p><p>クッション値 9.4</p><p>発表予定 10月18日 16時30分</p>",
    )
    info = _scrape(_Gets(gets={BABA_TOKYO: only_announcement, BABA_KYOTO: _baba("京都")}))
    assert info.measured_at is None
    assert info.is_today is None
    assert info.stale is None
    assert info.warning == JRATrackScraper.CUSHION_UNDATED
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.value == 9.4
    assert info.turf.cushion.measured_at is None


def test_same_line_next_announcement_keeps_the_measurement():
    seven = datetime(2026, 10, 18, 7, 0, tzinfo=TOKYO)
    assert page_measurement_time("10月18日 7時00分 測定（次回発表 12時）", 2026) == seven
    assert (
        page_measurement_time("10月18日 7時00分 測定（次回発表 12時30分）", 2026) == seven
    )
    assert page_measurement_time("10月18日 5時30分 測定 発走は15時40分", 2026) == datetime(
        2026, 10, 18, 5, 30, tzinfo=TOKYO
    )
    assert page_measurement_time("10月18日 7時00分発表予定", 2026) is None
    assert page_measurement_time(
        "10月18日 15時40分 ブラジルC 発走 10月18日 16時00分 測定", 2026
    ) == datetime(2026, 10, 18, 16, 0, tzinfo=TOKYO)
    html = _baba(
        "東京",
        """
        <p>2026年10月18日（日曜） 東京競馬場</p>
        <h2>クッション値</h2>
        <div class="content" id="cushion_num"><p><strong>9.1</strong></p></div>
        <p>10月18日 7時00分 測定（次回発表 12時）</p>
        """,
    )
    info = _scrape(_Gets(gets={BABA_TOKYO: html, BABA_KYOTO: _baba("京都")}))
    assert info.measured_at == seven
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.value == 9.1
    assert info.turf.cushion.measured_at == seven
    assert info.turf.cushion.source == "jra_html"
    assert info.turf.cushion.fetched_at == PINNED
    assert info.warning is None


def test_unlisted_heading_does_not_leak_into_the_moisture_clock():
    five_thirty = datetime(2026, 10, 18, 5, 30, tzinfo=TOKYO)
    html = _baba(
        "東京",
        """
        <p>2026年10月18日（日曜） 東京競馬場</p>
        <h2>クッション値</h2>
        <div class="content" id="cushion_num"><p><strong>9.1</strong></p></div>
        <p>10月18日 5時30分</p>
        <h2>含水率</h2>
        <table>
          <tr><th></th><th>ゴール前</th><th>4コーナー</th></tr>
          <tr><td>芝</td><td>14.7</td><td>12.3</td></tr>
          <tr><td>ダート</td><td>1.7</td><td>1.5</td></tr>
          <tr><td>計測情報</td><td>参考</td><td></td></tr>
        </table>
        <h4>測定時刻</h4>
        <select><option>10月18日 5時30分</option></select>
        <h2>散水情報</h2>
        <p>10月18日 8時</p>
        <h2>開催概要 10月18日 9時</h2>
        """,
    )
    info = _scrape(_Gets(gets={BABA_TOKYO: html, BABA_KYOTO: _baba("京都")}))
    assert info.measured_at == five_thirty
    assert info.turf is not None and info.turf.moisture is not None
    assert info.turf.moisture.goal_front == 14.7
    assert info.turf.moisture.fourth_corner == 12.3
    assert info.turf.moisture.measured_at == five_thirty
    assert info.turf.moisture.source == "jra_html"
    assert info.turf.moisture.fetched_at == PINNED
    assert info.dirt is not None and info.dirt.moisture is not None
    assert info.dirt.moisture.goal_front == 1.7
    assert info.dirt.moisture.fourth_corner == 1.5
    assert info.dirt.moisture.measured_at == five_thirty
    assert info.warning is None


def test_rendered_baba_page_reads_the_dropdown_clock():
    """Public index.html after the page's own script fills the measurement lists.

    Saved from ``render_headless`` (browser UA, no-cache, stop on 401/403).
    The h4 測定時刻 stays inside クッション値 and 含水率.
    """
    html = (FIXTURES / "jra_baba_rendered_index.html").read_text(encoding="utf-8")
    assert 'id="cushion_list"' in html and 'id="moist_list"' in html
    assert "測定時刻" in html
    info = _scrape(_Gets(gets={BABA_TOKYO: html, BABA_KYOTO: _baba("京都")}))
    seven = datetime(2026, 10, 18, 7, 0, tzinfo=TOKYO)
    five_thirty = datetime(2026, 10, 18, 5, 30, tzinfo=TOKYO)
    assert info.racecourse == "東京"
    assert info.measured_at == seven
    assert info.stale is False
    assert info.warning is None
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.value == 9.2
    assert info.turf.cushion.measured_at == seven
    assert info.turf.cushion.source == "jra_html"
    assert info.turf.cushion.fetched_at == PINNED
    assert info.turf.moisture is not None
    assert info.turf.moisture.goal_front == 14.7
    assert info.turf.moisture.fourth_corner == 12.3
    assert info.turf.moisture.measured_at == five_thirty
    assert info.turf.moisture.source == "jra_html"
    assert info.turf.moisture.fetched_at == PINNED
    assert info.dirt is not None and info.dirt.moisture is not None
    assert info.dirt.moisture.goal_front == 1.7
    assert info.dirt.moisture.fourth_corner == 1.5
    assert info.dirt.moisture.measured_at == five_thirty


def test_wait_fresh_rejects_non_finite_seconds():
    service = TrackInfoService()
    with pytest.raises(ValueError, match="max_wait must be a finite number >= 0"):
        service.get_track_info(wait_fresh=True, max_wait=float("nan"))
    with pytest.raises(ValueError, match="retry_interval must be a finite number >= 0"):
        service.get_track_info(wait_fresh=True, retry_interval=float("-inf"))
