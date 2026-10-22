"""Cushion is the printed number. A JRA 403 falls back to netkeiba's public page."""

import argparse
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from backend.cli import cmd_race_card, cmd_track
from backend.models.race import RaceCard
from backend.scrapers.jra_track_scraper import JRATrackScraper
from backend.scrapers.netkeiba_scraper import NetkeibaScraper
from backend.scrapers.netkeiba_track_scraper import NetkeibaTrackScraper
from backend.scrapers.page_fallback import headless_browser_installed, render_headless
from backend.services.track_info_service import TrackInfoService

BABA_TOKYO = "https://www.jra.go.jp/keiba/baba/index.html"
BABA_KYOTO = "https://www.jra.go.jp/keiba/baba/index2.html"


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    error = requests.HTTPError(f"http {status}")
    error.response = response
    return error


class _Gets:
    """GET map. Raises the stored error, otherwise returns the body."""

    def __init__(self, gets: dict | None = None, errors: dict | None = None):
        self.gets = gets or {}
        self.errors = errors or {}
        self.urls: list[str] = []
        self.encodings: list[str | None] = []

    def get(self, url, params=None, headers=None, encoding=None):
        self.urls.append(url)
        self.encodings.append(encoding)
        if url in self.errors:
            raise self.errors[url]
        return self.gets.get(url, "")


def _json_without_clocks(info: object) -> str:
    """Drop ISO timestamps so ``T15:40:01`` is not the post time ``15:40``."""
    text = info.model_dump_json()  # type: ignore[attr-defined]
    return re.sub(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2})?",
        "",
        text,
    )


def _baba(venue: str, body: str = "") -> str:
    return (
        "<html><head><title>馬場情報（"
        f"{venue}競馬場）　JRA</title></head><body>{body}</body></html>"
    )


def _tokyo_measured() -> str:
    return _baba(
        "東京",
        """
        <nav>函館競馬場 京都競馬場 札幌競馬場</nav>
        <p>クッション値 9.8</p>
        <table>
          <tr><th></th><th>ゴール前</th><th>4コーナー</th></tr>
          <tr><td>芝</td><td>12.1</td><td>11.4</td></tr>
          <tr><td>ダート</td><td>8.2</td><td>9.0</td></tr>
        </table>
        """,
    )


TOKYO = timezone(timedelta(hours=9))
PINNED = datetime(2026, 10, 12, 9, 0, tzinfo=TOKYO)
BABA_JS = "https://www.jra.go.jp/keiba/baba/_js/baba2025.js"
CUSHION_URL = "https://www.jra.go.jp/keiba/baba/_data_cushion.html"
MOIST_URL = "https://www.jra.go.jp/keiba/baba/_data_moist.html"
ACCESSJ = "https://www.jra.go.jp/JRADB/accessJ.html"
NK_TRACK = "https://race.netkeiba.com/race/track.html?jyo=05&kaisai_date=20261012"
NK_TRACK_OPEN = "https://race.netkeiba.com/race/track.html?jyo=05"
NK_API = "https://race.netkeiba.com/api/track_result?jyo=05"

BABA_SCRIPT = """
url: '_data_cushion.html',
url: '_data_moist.html',
url: '/JRADB/accessJ.html',
"""

CUSHION_FRAGMENT = """
<div id="rcA" title="東京">
  <div class="unit">
    <div class="time">10月12日（月曜）7時00分</div>
    <div class="cushion">9.6</div>
  </div>
  <div class="unit"><div class="cushion">9.4</div></div>
</div>
<div id="rcB" title="京都">
  <div class="unit"><div class="cushion">10.7</div></div>
</div>
"""

MOIST_FRAGMENT = """
<div id="rcA" title="東京">
  <div class="unit">
    <div class="turf"><span class="mg">13.2</span><span class="m4c">11.8</span></div>
    <div class="dirt"><span class="mg">1.7</span><span class="m4c">1.7</span></div>
  </div>
</div>
"""


def _scale_page(*, cushion_cell: str = "", moisture: str | None = None, script: bool = True) -> str:
    """Tokyo baba tab. The bar and the legend are not the reading."""
    cells = moisture
    if cells is None:
        cells = """
        <tr><td>芝</td><td>14.0</td><td>12.7</td></tr>
        <tr><td>ダート</td><td>2.1</td><td>2.2</td></tr>
        """
    script_tag = '<script src="/keiba/baba/_js/baba2025.js"></script>' if script else ""
    return _baba(
        "東京",
        f"""
        <div class="content" id="cushion_num"><p><strong>{cushion_cell}</strong></p></div>
        <ul>
          <li class="break">12</li><li class="break">10</li>
          <li class="break">8</li><li class="break">7</li>
        </ul>
        <table id="cushion_relation">
          <tr><td>12以上</td><td>硬め</td></tr>
          <tr><td>10から12</td><td>やや硬め</td></tr>
          <tr><td>8から10</td><td>標準</td></tr>
          <tr><td>7から8</td><td>やや軟らかめ</td></tr>
          <tr><td>7以下</td><td>軟らかめ</td></tr>
        </table>
        <table id="turf_line">
          <tr><th></th><th>ゴール前</th><th>4コーナー</th></tr>
          {cells}
        </table>
        <p>10月12日（月曜）7時00分</p>
        {script_tag}
        """,
    )


def _nk_tokyo(*, cushion: str = "9.6", kyoto_first: bool = False) -> str:
    """Active Tokyo panel. Kyoto's number sits in another panel."""
    tokyo = f"""
    <span class="TrackTabArea00 Jyo_05">
      <div class="TrackTurf01">
        <div class="CourseData">天候：晴 馬場：良</div>
        <div class="CushionData">{cushion}</div>
        <ul class="CushionBar"><li class="BarPoint">12</li><li class="BarPoint">10</li></ul>
        <div class="CushionTypeTxt">硬め</div>
        <div class="MoistureWrap01">
          <dl><dt>4コーナー</dt><dd>11.8</dd></dl>
          <dl><dt>ゴール前</dt><dd>13.2</dd></dl>
        </div>
        <p>15:40発走</p>
      </div>
      <div class="TrackDirt01"><div class="CourseData">馬場：稍重</div></div>
    </span>
    """
    kyoto = """
    <span class="TrackTabArea00 Jyo_08">
      <div class="TrackTurf01"><div class="CushionData">10.7</div></div>
    </span>
    """
    panels = kyoto + tokyo if kyoto_first else tokyo + kyoto
    return f"""
    <html><body>
      <ul><li class="Active" data-kaisaijyo="Jyo_05">東京</li></ul>
      {panels}
    </body></html>
    """


def _boom(url: str) -> str:
    raise AssertionError(f"headless should not run for {url}")


def test_jra_empty_slot_reads_the_fragment_not_the_scale():
    client = _Gets(
        gets={
            BABA_TOKYO: _scale_page(),
            BABA_KYOTO: _baba("京都"),
            BABA_JS: BABA_SCRIPT,
            CUSHION_URL: CUSHION_FRAGMENT,
            MOIST_URL: MOIST_FRAGMENT,
        }
    )
    info = JRATrackScraper(http_client=client, headless_render=_boom).scrape(  # type: ignore[arg-type]
        "tokyo", fetched_at=PINNED
    )
    assert info.racecourse == "東京"
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.value == 9.6
    assert info.turf.cushion.grade is None
    assert info.turf.cushion.source == "jra_js"
    assert info.turf.cushion.fetched_at == PINNED
    assert info.turf.moisture is not None
    assert info.turf.moisture.goal_front == 14.0
    assert info.turf.moisture.fourth_corner == 12.7
    assert info.turf.moisture.source == "jra_html"
    assert info.turf.moisture.fetched_at == PINNED
    assert info.dirt is not None and info.dirt.moisture is not None
    assert info.dirt.moisture.goal_front == 2.1
    assert info.dirt.moisture.fourth_corner == 2.2
    assert info.post_time is None
    assert ACCESSJ not in client.urls
    assert client.urls == [BABA_TOKYO, BABA_KYOTO, BABA_JS, CUSHION_URL, MOIST_URL]
    assert set(client.encodings) == {"cp932"}


def test_printed_grade_stays_with_the_number():
    client = _Gets(
        gets={
            BABA_TOKYO: _scale_page(cushion_cell="9.6（標準）", script=True),
            BABA_KYOTO: _baba("京都"),
        }
    )
    info = JRATrackScraper(http_client=client, headless_render=_boom).scrape(  # type: ignore[arg-type]
        "tokyo", fetched_at=PINNED
    )
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.value == 9.6
    assert info.turf.cushion.grade == "標準"
    assert info.turf.cushion.source == "jra_html"
    assert info.turf.cushion.fetched_at == PINNED
    assert BABA_JS not in client.urls


def test_missing_cushion_number_is_null_and_warned():
    client = _Gets(
        gets={
            BABA_TOKYO: _scale_page(script=False),
            BABA_KYOTO: _baba("京都"),
        }
    )
    info = JRATrackScraper(http_client=client, headless_render="off").scrape(  # type: ignore[arg-type]
        "tokyo", fetched_at=PINNED
    )
    assert info.turf is not None
    assert info.turf.cushion is None
    assert info.warning == JRATrackScraper.CUSHION_MISSING
    assert info.turf.moisture is not None
    assert info.turf.moisture.goal_front == 14.0
    assert client.urls == [BABA_TOKYO, BABA_KYOTO]
    assert "硬め" not in (info.model_dump_json())


def test_empty_moisture_cells_are_filled_from_the_fragment():
    empty = """
    <tr><td>芝</td><td></td><td></td></tr>
    <tr><td>ダート</td><td></td><td></td></tr>
    """
    client = _Gets(
        gets={
            BABA_TOKYO: _scale_page(moisture=empty),
            BABA_KYOTO: _baba("京都"),
            BABA_JS: BABA_SCRIPT,
            CUSHION_URL: CUSHION_FRAGMENT,
            MOIST_URL: MOIST_FRAGMENT,
        }
    )
    info = JRATrackScraper(http_client=client, headless_render=_boom).scrape(  # type: ignore[arg-type]
        "tokyo", fetched_at=PINNED
    )
    assert info.turf is not None and info.turf.moisture is not None
    assert info.turf.moisture.goal_front == 13.2
    assert info.turf.moisture.fourth_corner == 11.8
    assert info.turf.moisture.source == "jra_js"
    assert info.turf.moisture.fetched_at == PINNED
    assert info.dirt is not None and info.dirt.moisture is not None
    assert info.dirt.moisture.goal_front == 1.7


def test_cushion_fragment_403_stops_before_headless():
    client = _Gets(
        gets={
            BABA_TOKYO: _scale_page(),
            BABA_KYOTO: _baba("京都"),
            BABA_JS: BABA_SCRIPT,
        },
        errors={CUSHION_URL: _http_error(403)},
    )
    info = JRATrackScraper(http_client=client, headless_render=_boom).scrape("tokyo")  # type: ignore[arg-type]
    assert info.racecourse == "東京"
    assert info.turf is not None
    assert info.turf.cushion is None
    assert info.turf.moisture is not None
    assert info.turf.moisture.goal_front == 14.0
    assert info.warning is not None
    assert "403" in info.warning
    assert JRATrackScraper.CUSHION_MISSING in info.warning
    assert MOIST_URL not in client.urls
    assert ACCESSJ not in client.urls


def test_jra_403_falls_back_to_the_netkeiba_public_page():
    jra = _Gets(errors={BABA_TOKYO: _http_error(403), BABA_KYOTO: _http_error(403)})
    netkeiba = _Gets(gets={NK_TRACK: _nk_tokyo(kyoto_first=True)})
    service = TrackInfoService(
        jra_scraper=JRATrackScraper(http_client=jra, headless_render=_boom),  # type: ignore[arg-type]
        netkeiba_scraper=NetkeibaTrackScraper(  # type: ignore[arg-type]
            http_client=netkeiba, headless_render=_boom
        ),
    )
    results = service.get_track_info(racecourse="東京", date="20261012", sources=["jra"])
    assert set(results) == {"jra", "netkeiba"}
    assert results["jra"].warning == JRATrackScraper.BABA_403
    assert results["jra"].racecourse is None
    assert results["jra"].turf is not None
    assert results["jra"].turf.cushion is None
    assert jra.urls == [BABA_TOKYO, BABA_KYOTO]
    copied = results["netkeiba"]
    assert copied.racecourse == "東京"
    assert copied.turf is not None and copied.turf.cushion is not None
    assert copied.turf.cushion.value == 9.6
    assert copied.turf.cushion.grade is None
    assert copied.turf.cushion.source == "netkeiba_html"
    assert copied.turf.cushion.fetched_at is not None
    assert copied.turf.cushion.fetched_at.utcoffset() == timedelta(hours=9)
    assert copied.turf.moisture is not None
    assert copied.turf.moisture.goal_front == 13.2
    assert copied.turf.moisture.fourth_corner == 11.8
    assert copied.turf.moisture.source == "netkeiba_html"
    assert copied.turf.condition.value == "良"
    assert copied.turf.condition_source == "netkeiba_html"
    assert copied.dirt is not None
    assert copied.dirt.condition.value == "稍重"
    assert copied.weather.value == "晴"
    assert copied.weather_source == "netkeiba_html"
    assert copied.post_time == "15:40+09:00"
    assert copied.post_time_source == "netkeiba_html"
    assert copied.post_time_fetched_at == copied.turf.cushion.fetched_at
    assert "10.7" not in _json_without_clocks(copied)
    assert netkeiba.urls == [NK_TRACK]


def test_successful_jra_cushion_does_not_call_netkeiba():
    jra = _Gets(gets={BABA_TOKYO: _tokyo_measured(), BABA_KYOTO: _baba("京都")})
    netkeiba = _Gets(gets={NK_TRACK_OPEN: _nk_tokyo()})
    service = TrackInfoService(
        jra_scraper=JRATrackScraper(http_client=jra, headless_render=_boom),  # type: ignore[arg-type]
        netkeiba_scraper=NetkeibaTrackScraper(  # type: ignore[arg-type]
            http_client=netkeiba, headless_render=_boom
        ),
    )
    results = service.get_track_info(racecourse="東京", sources=["jra"])
    assert set(results) == {"jra"}
    assert results["jra"].turf is not None and results["jra"].turf.cushion is not None
    assert results["jra"].turf.cushion.value == 9.8
    assert results["jra"].turf.cushion.source == "jra_html"
    assert netkeiba.urls == []


def test_script_date_does_not_become_the_meeting_day():
    script_only = """
    <html><body>
      <script>var built = "2026/04/02";</script>
      <ul><li class="Active">東京</li></ul>
      <span class="TrackTabArea00 Jyo_05">
        <div class="TrackTurf01"><div class="CourseData">天候：晴 馬場：良</div></div>
      </span>
    </body></html>
    """
    chosen = """
    <html><body>
      <script>var built = "2026/04/02";</script>
      <a class="Active" href="track.html?kaisai_date=20261018">10月18日</a>
      <ul><li class="Active">東京</li></ul>
      <span class="TrackTabArea00 Jyo_05">
        <div class="TrackTurf01"><div class="CourseData">天候：晴 馬場：良</div></div>
      </span>
    </body></html>
    """
    client = _Gets(gets={NK_TRACK_OPEN: script_only})
    info = NetkeibaTrackScraper(http_client=client, headless_render=_boom).scrape("05")  # type: ignore[arg-type]
    assert info.date is None
    assert info.racecourse == "東京"

    client = _Gets(gets={NK_TRACK_OPEN: chosen})
    info = NetkeibaTrackScraper(http_client=client, headless_render=_boom).scrape("05")  # type: ignore[arg-type]
    assert info.date == datetime(2026, 10, 18)
    assert info.racecourse == "東京"


def test_netkeiba_403_does_not_render_or_invent_a_number():
    client = _Gets(errors={NK_TRACK_OPEN: _http_error(403)})
    info = NetkeibaTrackScraper(http_client=client, headless_render=_boom).scrape("05")  # type: ignore[arg-type]
    assert info.warning == NetkeibaTrackScraper.TRACK_403
    assert info.http_status == 403
    assert info.login_required is False
    assert info.racecourse is None
    assert info.turf is not None
    assert info.turf.cushion is None
    assert info.post_time is None
    assert client.urls == [NK_TRACK_OPEN]


def test_netkeiba_login_wall_stops_the_chain():
    html = """
    <html><head><title>ログイン</title></head><body>
      <input type="password" name="password">
      <li class="Active">東京</li>
      <div class="CushionData">9.9</div>
      <p>15:40発走</p>
    </body></html>
    """
    client = _Gets(gets={NK_TRACK_OPEN: html})
    info = NetkeibaTrackScraper(http_client=client, headless_render=_boom).scrape("05")  # type: ignore[arg-type]
    assert info.warning == NetkeibaTrackScraper.TRACK_LOGIN
    assert info.racecourse is None
    assert info.post_time is None
    dumped = _json_without_clocks(info)
    assert "9.9" not in dumped
    assert "15:40" not in dumped
    stamped = info.model_copy(update={"scraped_at": datetime.fromisoformat("2026-10-22T15:40:01")})
    assert "15:40" not in _json_without_clocks(stamped)
    assert "15:40" in stamped.model_dump_json()


def test_embedded_json_supplies_the_number_and_a_printed_grade():
    html = """
    <html><body>
      <li class="Active">東京</li>
      <div class="TrackTurf01"><div class="CushionData"></div></div>
      <script type="application/json">{"cushion_value": 9.6, "grade": "標準"}</script>
    </body></html>
    """
    client = _Gets(gets={NK_TRACK_OPEN: html})
    info = NetkeibaTrackScraper(http_client=client, headless_render=_boom).scrape(  # type: ignore[arg-type]
        "05", fetched_at=PINNED
    )
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.value == 9.6
    assert info.turf.cushion.grade == "標準"
    assert info.turf.cushion.source == "netkeiba_js"
    assert info.turf.cushion.fetched_at == PINNED
    assert client.urls == [NK_TRACK_OPEN]


def test_json_number_does_not_invent_a_grade():
    html = """
    <html><body>
      <li class="Active">東京</li>
      <div class="TrackTurf01"><div class="CushionData"></div></div>
      <script type="application/json">{"cushion_value": 9.6}</script>
    </body></html>
    """
    info = NetkeibaTrackScraper(  # type: ignore[arg-type]
        http_client=_Gets(gets={NK_TRACK_OPEN: html}), headless_render=_boom
    ).scrape("05")
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.value == 9.6
    assert info.turf.cushion.grade is None
    assert info.turf.cushion.source == "netkeiba_js"


def test_same_site_url_the_page_calls_supplies_the_number():
    html = """
    <html><body>
      <li class="Active">東京</li>
      <div class="TrackTurf01"><div class="CushionData"></div></div>
      <script>url: '/api/track_result?jyo=05'</script>
    </body></html>
    """
    client = _Gets(
        gets={
            NK_TRACK_OPEN: html,
            NK_API: '{"cushion_value": 9.6}',
        }
    )
    info = NetkeibaTrackScraper(http_client=client, headless_render=_boom).scrape(  # type: ignore[arg-type]
        "05", fetched_at=PINNED
    )
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.value == 9.6
    assert info.turf.cushion.source == "netkeiba_js"
    assert info.turf.cushion.fetched_at == PINNED
    assert client.urls == [NK_TRACK_OPEN, NK_API]


def test_headless_render_fills_an_empty_cushion_slot():
    html = """
    <html><body>
      <li class="Active">東京</li>
      <div class="TrackTurf01"><div class="CushionData"></div></div>
    </body></html>
    """
    seen: list[str] = []

    def render(url: str) -> str:
        seen.append(url)
        return """
        <html><body>
          <div class="TrackTurf01"><div class="CushionData">9.6</div></div>
        </body></html>
        """

    info = NetkeibaTrackScraper(  # type: ignore[arg-type]
        http_client=_Gets(gets={NK_TRACK_OPEN: html}),
        headless_render=render,
    ).scrape("05", fetched_at=PINNED)
    assert seen == [NK_TRACK_OPEN]
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.value == 9.6
    assert info.turf.cushion.grade is None
    assert info.turf.cushion.source == "headless"
    assert info.turf.cushion.fetched_at == PINNED


def test_missing_headless_browser_warns_and_leaves_cushion_null(monkeypatch):
    monkeypatch.setattr(
        "backend.scrapers.page_fallback.headless_browser_installed",
        lambda: False,
    )
    html = """
    <html><body>
      <li class="Active">東京</li>
      <div class="TrackTurf01"><div class="CushionData"></div></div>
    </body></html>
    """
    info = NetkeibaTrackScraper(  # type: ignore[arg-type]
        http_client=_Gets(gets={NK_TRACK_OPEN: html}),
        headless_render="auto",
    ).scrape("05")
    assert info.turf is not None
    assert info.turf.cushion is None
    assert info.warning is not None
    assert "headless browser is not installed" in info.warning


@pytest.mark.skipif(
    not headless_browser_installed(),
    reason="headless browser is not installed; no CI workflow installs Playwright",
)
def test_installed_headless_browser_renders_a_local_track_page(tmp_path: Path):
    page = tmp_path / "track.html"
    page.write_text(
        "<html><body><div class='CushionData'>9.6</div><p>15:40発走</p></body></html>",
        encoding="utf-8",
    )
    rendered = render_headless(page.as_uri())
    assert "9.6" in rendered
    assert "15:40発走" in rendered


def test_shutuba_post_time_uses_the_tokyo_offset():
    html = """
    <html><body>
      <h1 class="RaceName">毎日王冠</h1>
      <div class="RaceData01">15:45発走 / 芝1800m / 天候:曇 / 馬場:良</div>
      <div class="RaceData02"><span>4回</span><span>東京</span><span>2日目</span></div>
    </body></html>
    """
    url = f"{NetkeibaScraper.BASE_URL}/race/shutuba.html"
    scraper = NetkeibaScraper(http_client=_Gets(gets={url: html}))  # type: ignore[arg-type]
    card = scraper.get_race_card("202605040211")
    assert card is not None
    assert card.race.post_time == "15:45+09:00"
    assert card.race.post_time_source == "netkeiba_html"
    assert card.race.post_time_fetched_at is not None
    assert card.race.post_time_fetched_at.utcoffset() == timedelta(hours=9)

    bare = html.replace("15:45発走", "15:45")
    quiet = NetkeibaScraper(http_client=_Gets(gets={url: bare})).get_race_card(  # type: ignore[arg-type]
        "202605040211"
    )
    assert quiet is not None
    assert quiet.race.post_time is None
    assert quiet.race.post_time_source is None


def test_cli_prints_post_time_on_the_race_card(capsys, monkeypatch):
    html = """
    <html><body>
      <h1 class="RaceName">毎日王冠</h1>
      <div class="RaceData01">15:45発走 / 芝1800m / 天候:曇</div>
      <div class="RaceData02"><span>4回</span><span>東京</span><span>2日目</span></div>
    </body></html>
    """
    url = f"{NetkeibaScraper.BASE_URL}/race/shutuba.html"
    scraper = NetkeibaScraper(http_client=_Gets(gets={url: html}))  # type: ignore[arg-type]
    card = scraper.get_race_card("202605040211")
    assert isinstance(card, RaceCard)

    class _Service:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_race_card(self, race_id: str) -> RaceCard:
            assert race_id == "202605040211"
            return card

    monkeypatch.setattr("backend.cli.RaceService", lambda: _Service())
    cmd_race_card(argparse.Namespace(race_id="202605040211", json=False))
    text = capsys.readouterr().out
    assert "発走: 15:45+09:00" in text
    assert "netkeiba_html" in text

    cmd_race_card(argparse.Namespace(race_id="202605040211", json=True))
    payload = capsys.readouterr().out
    assert '"post_time": "15:45+09:00"' in payload
    assert '"post_time_source": "netkeiba_html"' in payload


def test_cli_track_text_prints_cushion_grade_and_post_time(capsys, monkeypatch):
    jra = _Gets(errors={BABA_TOKYO: _http_error(403), BABA_KYOTO: _http_error(403)})
    netkeiba = _Gets(gets={NK_TRACK_OPEN: _nk_tokyo()})
    service = TrackInfoService(
        jra_scraper=JRATrackScraper(http_client=jra, headless_render="off"),  # type: ignore[arg-type]
        netkeiba_scraper=NetkeibaTrackScraper(  # type: ignore[arg-type]
            http_client=netkeiba, headless_render="off"
        ),
    )
    monkeypatch.setattr("backend.cli.TrackInfoService", lambda: service)
    cmd_track(argparse.Namespace(venue="東京", date=None, merge=False, json=False))
    text = capsys.readouterr().out
    assert "クッション値: 9.6" in text
    assert "硬め" not in text.split("クッション値: 9.6", 1)[1].split("\n", 1)[0]
    assert "発走: 15:40+09:00" in text
    assert "netkeiba_html" in text
    assert "JRA baba returned 403" in text
