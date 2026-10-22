"""Race-day baba freshness: printed clock, stale warning, and an uncached live URL."""

from datetime import date, datetime
from pathlib import Path

import requests

from backend.scrapers.jra_track_scraper import JRATrackScraper
from backend.scrapers.page_cache import CachingHTTPClient
from backend.scrapers.track_freshness import (
    CACHED_COPY,
    RACE_DATE_UNREADABLE,
    STALE_PREFIX,
    cached_copy_warning,
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
    assert info.warning is None


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
    assert results["jra"].turf is None or results["jra"].turf.cushion is None
    assert client.urls == [BABA_TOKYO]
