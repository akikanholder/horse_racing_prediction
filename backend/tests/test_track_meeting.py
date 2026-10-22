"""Venue is copied only from the requested meeting. 403 stays a null TrackInfo."""

import argparse
import asyncio
import json
import re
from datetime import datetime

import requests
from fastapi.testclient import TestClient

import backend.main as main
from backend.cli import cmd_track
from backend.models.race import RaceCard
from backend.models.track import empty_track_info
from backend.scrapers.jra_track_scraper import JRATrackScraper
from backend.scrapers.netkeiba_scraper import NetkeibaScraper
from backend.scrapers.netkeiba_track_scraper import NetkeibaTrackScraper
from backend.services.track_info_service import TrackInfoService

TOKYO_DAY2 = "202605040211"  # 2026 東京 4回 2日 11R。日次 02 は函館ではない
KYOTO_DAY2 = "202608040211"  # 2026 京都 4回 2日 11R
BABA_TOKYO = "https://www.jra.go.jp/keiba/baba/index.html"
BABA_KYOTO = "https://www.jra.go.jp/keiba/baba/index2.html"
BABA_NIIGATA = "https://www.jra.go.jp/keiba/baba/index3.html"


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


def _forbidden_body() -> str:
    return (
        "<html><head><title>Forbidden　JRA</title></head><body>"
        "<a>札幌競馬場</a><a>函館競馬場</a><a>東京競馬場</a>"
        "<p>クッション値 9.9</p>"
        "<table><tr><td>芝</td><td>ゴール前</td><td>13.0</td>"
        "<td>4コーナー</td><td>14.0</td></tr></table>"
        "</body></html>"
    )


def _shutuba(venue: str, going: str | None = None) -> str:
    going_html = f" / 馬場:{going}" if going else ""
    return f"""
    <html><head><title>出馬表</title></head><body>
    <nav><a>函館競馬場</a><a>東京</a><a>京都</a></nav>
    <h1 class="RaceName">毎日王冠</h1>
    <div class="RaceData01">15:45発走 / 芝1800m / 天候:曇{going_html}</div>
    <div class="RaceData02">
      <span>4回</span><span>{venue}</span><span>2日目</span>
    </div>
    <div class="odds">単勝 3.4 1番人気</div>
    </body></html>
    """


def _json_without_clocks(info: object) -> str:
    """Drop ISO timestamps. ``19.804919`` must not look like the reading 9.8."""
    text = info.model_dump_json()  # type: ignore[attr-defined]
    return re.sub(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2})?",
        "",
        text,
    )


def _assert_null_measurement(info) -> None:
    payload = json.loads(info.model_dump_json())
    assert payload["racecourse"] is None
    assert payload["date"] is None
    assert payload["turf"]["cushion"] is None
    assert payload["turf"]["moisture"] is None
    assert payload["dirt"]["moisture"] is None
    assert payload["warning"]
    blob = _json_without_clocks(info)
    assert "函館" not in blob
    assert "9.9" not in blob
    assert "9.8" not in blob
    assert "8.1" not in blob


def test_jra_403_is_trackinfo_with_null_venue_and_measurements():
    client = _Gets(errors={BABA_TOKYO: _http_error(403), BABA_KYOTO: _http_error(403)})
    info = JRATrackScraper(http_client=client).scrape("tokyo")  # type: ignore[arg-type]
    _assert_null_measurement(info)
    assert info.warning == JRATrackScraper.BABA_403
    assert info.http_status == 403
    assert info.login_required is False
    assert info.source == "JRA"
    assert client.urls == [BABA_TOKYO, BABA_KYOTO]
    assert client.encodings == ["cp932", "cp932"]
    assert all("tokyo.html" not in url for url in client.urls)


def test_jra_non_html_body_does_not_copy_hakodate_or_invent_cushion():
    client = _Gets(
        gets={BABA_TOKYO: _forbidden_body(), BABA_KYOTO: "not html 函館競馬場 クッション値 7.7"}
    )
    info = JRATrackScraper(http_client=client).scrape("tokyo")  # type: ignore[arg-type]
    _assert_null_measurement(info)
    assert info.warning == JRATrackScraper.BABA_UNPARSEABLE
    assert "7.7" not in _json_without_clocks(info)


def test_jra_other_meeting_is_not_copied():
    client = _Gets(
        gets={
            BABA_TOKYO: _baba("函館", "<p>クッション値 8.1</p><p>ゴール前 10.0</p>"),
            BABA_KYOTO: _baba("京都", "<p>クッション値 8.4</p>"),
        }
    )
    info = JRATrackScraper(http_client=client).scrape("tokyo")  # type: ignore[arg-type]
    _assert_null_measurement(info)
    assert info.warning == JRATrackScraper.BABA_NOT_MEETING
    assert info.racecourse != "函館"
    assert info.racecourse != "京都"


def test_jra_matching_meeting_copies_venue_and_only_parsed_numbers():
    client = _Gets(gets={BABA_TOKYO: _tokyo_measured(), BABA_KYOTO: _baba("京都")})
    info = JRATrackScraper(http_client=client).scrape("tokyo")  # type: ignore[arg-type]
    assert info.racecourse == "東京"
    assert info.warning is None
    assert info.turf is not None
    assert info.turf.cushion is not None
    assert info.turf.cushion.value == 9.8
    assert info.turf.moisture is not None
    assert info.turf.moisture.goal_front == 12.1
    assert info.turf.moisture.fourth_corner == 11.4
    assert info.dirt is not None
    assert info.dirt.moisture is not None
    assert info.dirt.moisture.goal_front == 8.2

    bare = _Gets(gets={BABA_TOKYO: _baba("東京", "<p>使用コース A</p>"), BABA_KYOTO: _baba("京都")})
    quiet = JRATrackScraper(http_client=bare).scrape("kyoto")  # type: ignore[arg-type]
    # Kyoto is on the second tab. Tokyo's page must not supply Kyoto's numbers.
    assert quiet.racecourse == "京都"
    assert quiet.turf is not None
    assert quiet.turf.cushion is None
    assert quiet.turf.moisture is None
    assert quiet.dirt is not None
    assert quiet.dirt.moisture is None


def _with_meeting_tab(venue: str, body: str, tabs: list[tuple[str, str]]) -> str:
    tab_html = "".join(f'<div><a href="{href}">{label}</a></div>' for href, label in tabs)
    return _baba(
        venue,
        f'<div class="kaisai_tab"><div class="nav tab">{tab_html}</div></div>'
        f'<a href="/facilities/race/niigata/">新潟競馬場</a>{body}',
    )


def test_niigata_meeting_follows_the_kaisai_tab_not_a_venue_url():
    tokyo = _with_meeting_tab(
        "東京",
        "<p>クッション値 9.8</p>",
        [
            ("index.html", "東京競馬場"),
            ("index2.html", "京都競馬場"),
            ("index3.html", "新潟競馬場"),
        ],
    )
    niigata = _baba(
        "新潟",
        """
        <p>クッション値 10.2</p>
        <table>
          <tr><th></th><th>ゴール前</th><th>4コーナー</th></tr>
          <tr><td>芝</td><td>11.5</td><td>12.6</td></tr>
          <tr><td>ダート</td><td>5.5</td><td>6.3</td></tr>
        </table>
        """,
    )
    client = _Gets(
        gets={
            BABA_TOKYO: tokyo,
            BABA_KYOTO: _baba("京都", "<p>クッション値 8.4</p>"),
            BABA_NIIGATA: niigata,
        }
    )
    info = JRATrackScraper(http_client=client).scrape("niigata")  # type: ignore[arg-type]
    assert info.racecourse == "新潟"
    assert info.warning is None
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.value == 10.2
    assert info.turf.moisture is not None
    assert info.turf.moisture.goal_front == 11.5
    assert info.turf.moisture.fourth_corner == 12.6
    assert info.dirt is not None and info.dirt.moisture is not None
    assert info.dirt.moisture.goal_front == 5.5
    assert info.dirt.moisture.fourth_corner == 6.3
    assert client.urls == [BABA_TOKYO, BABA_KYOTO, BABA_NIIGATA]
    assert all("niigata.html" not in url and "facilities" not in url for url in client.urls)

    tokyo_only = _Gets(
        gets={
            BABA_TOKYO: _with_meeting_tab(
                "東京",
                "<p>クッション値 9.8</p>",
                [("index3.html", "新潟競馬場")],
            ),
            BABA_KYOTO: _baba("京都"),
            BABA_NIIGATA: niigata,
        }
    )
    kept = JRATrackScraper(http_client=tokyo_only).scrape("tokyo")  # type: ignore[arg-type]
    assert kept.racecourse == "東京"
    assert tokyo_only.urls == [BABA_TOKYO, BABA_KYOTO]


def test_service_and_cli_emit_trackinfo_json_when_both_sources_fail(capsys, monkeypatch):
    jra_client = _Gets(errors={BABA_TOKYO: _http_error(403), BABA_KYOTO: _http_error(403)})
    netkeiba_url = "https://race.netkeiba.com/race/track.html?jyo=05"
    netkeiba_client = _Gets(errors={netkeiba_url: _http_error(403)})
    service = TrackInfoService(
        jra_scraper=JRATrackScraper(http_client=jra_client),  # type: ignore[arg-type]
        netkeiba_scraper=NetkeibaTrackScraper(http_client=netkeiba_client),  # type: ignore[arg-type]
    )
    results = service.get_track_info(racecourse="東京", sources=["jra", "netkeiba"])
    payload = json.loads(
        json.dumps({key: value.model_dump(mode="json") for key, value in results.items()})
    )
    assert set(payload) == {"jra", "netkeiba"}
    for source in payload.values():
        assert source["racecourse"] is None
        assert source["turf"]["cushion"] is None
        assert source["turf"]["moisture"] is None
        assert source["dirt"]["moisture"] is None
        assert source["warning"]
    assert "函館" not in json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr("backend.cli.TrackInfoService", lambda: service)
    cmd_track(argparse.Namespace(venue="東京", date=None, merge=False, json=True))
    stdout = capsys.readouterr().out
    assert not stdout.lstrip().startswith("=")
    parsed = json.loads(stdout)
    assert parsed["jra"]["warning"] == JRATrackScraper.BABA_403
    assert parsed["jra"]["turf"]["cushion"] is None
    assert "cushion" in parsed["jra"]["turf"]
    assert "moisture" in parsed["jra"]["turf"]
    assert parsed["netkeiba"]["warning"] == NetkeibaTrackScraper.TRACK_403


def test_api_returns_trackinfo_json_on_403(monkeypatch):
    class _Blocked:
        def get_track_info(self, **kwargs):
            return {"jra": empty_track_info("JRA", JRATrackScraper.BABA_403)}

    monkeypatch.setattr(main, "track_info_service", _Blocked())
    result = asyncio.run(main.get_track_info(venue="東京"))
    body = {key: value.model_dump(mode="json") for key, value in result.items()}
    assert body["jra"]["racecourse"] is None
    assert body["jra"]["turf"]["cushion"] is None
    assert body["jra"]["turf"]["moisture"] is None
    assert body["jra"]["warning"] == JRATrackScraper.BABA_403

    class _AppService:
        def close(self):
            return None

        def get_track_info(self, **kwargs):
            return {"jra": empty_track_info("JRA", JRATrackScraper.BABA_403)}

    monkeypatch.setattr(main, "TrackInfoService", _AppService)
    monkeypatch.setattr(main, "RaceService", _AppService)
    with TestClient(main.app) as client:
        response = client.get("/api/tracks", params={"venue": "東京"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert payload["jra"]["racecourse"] is None
    assert payload["jra"]["turf"]["cushion"] is None
    assert payload["jra"]["turf"]["moisture"] is None
    assert payload["jra"]["warning"] == JRATrackScraper.BABA_403


def test_shutuba_403_body_does_not_set_track_bias_venue():
    html = _forbidden_body()
    card = _card(html, TOKYO_DAY2)
    bias = card.race.track_bias
    assert bias is not None
    assert card.race.venue == "不明"
    assert bias.venue is None
    assert bias.going is None
    assert bias.inside_outside is None
    assert bias.pace_bias is None
    assert bias.source is None
    assert bias.data_sufficiency == "unavailable"


def test_shutuba_for_a_different_meeting_does_not_copy_that_venue():
    card = _card(_shutuba("函館", going="良"), TOKYO_DAY2)
    bias = card.race.track_bias
    assert bias is not None
    assert bias.venue is None
    assert bias.going is None
    assert card.race.venue == "不明"
    assert "函館" not in (bias.model_dump_json())


def test_shutuba_for_the_requested_meeting_copies_venue_not_the_day_code():
    tokyo = _card(_shutuba("東京", going="良"), TOKYO_DAY2)
    bias = tokyo.race.track_bias
    assert bias is not None
    assert tokyo.race.venue == "東京"
    assert tokyo.race.venue_code == "05"
    assert bias.venue == "東京"
    assert bias.going == "良"
    assert bias.surface == "芝"
    assert bias.distance == 1800
    assert bias.inside_outside is None
    assert bias.pace_bias is None
    assert bias.source is None

    kyoto = _card(_shutuba("京都"), KYOTO_DAY2)
    assert kyoto.race.track_bias is not None
    assert kyoto.race.venue == "京都"
    assert kyoto.race.track_bias.venue == "京都"
    assert kyoto.race.track_bias.going is None

    quiet = _card(_shutuba("東京"), TOKYO_DAY2)
    assert quiet.race.track_bias is not None
    assert quiet.race.track_bias.venue == "東京"
    assert quiet.race.track_bias.going is None


def test_project_race_id_still_uses_its_own_venue_code():
    html = _shutuba("中山", going="稍重")
    card = _card(html, "202609260611")
    assert card.race.venue == "中山"
    assert card.race.track_bias is not None
    assert card.race.track_bias.venue == "中山"
    assert card.race.track_bias.going == "稍重"


def test_null_measurement_check_ignores_a_clock_that_looks_like_a_reading():
    info = empty_track_info("JRA", JRATrackScraper.BABA_403).model_copy(
        update={"scraped_at": datetime.fromisoformat("2026-10-22T15:38:19.804919")}
    )
    _assert_null_measurement(info)
    assert "9.8" in info.model_dump_json()


def test_netkeiba_track_rejects_another_tabs_numbers():
    html = """
    <html><body>
      <ul>
        <li class="Active">函館</li>
        <li>東京</li>
      </ul>
      <p>クッション値</p>
      <div>9.1</div>
    </body></html>
    """
    client = _Gets(gets={"https://race.netkeiba.com/race/track.html?jyo=05": html})
    info = NetkeibaTrackScraper(http_client=client).scrape("05")  # type: ignore[arg-type]
    _assert_null_measurement(info)
    assert info.warning == NetkeibaTrackScraper.TRACK_NOT_MEETING


def test_netkeiba_track_copies_the_active_tab_when_it_is_the_request():
    html = """
    <html><body>
      <ul>
        <li>函館</li>
        <li class="Active">東京</li>
        <li class="Active">芝</li>
      </ul>
      <div>クッション値
9.7
      </div>
    </body></html>
    """
    url = "https://race.netkeiba.com/race/track.html?jyo=05"
    info = NetkeibaTrackScraper(http_client=_Gets(gets={url: html})).scrape("05")  # type: ignore[arg-type]
    assert info.racecourse == "東京"
    assert info.warning is None
    assert info.turf is not None
    assert info.turf.cushion is not None
    assert info.turf.cushion.value == 9.7


def test_empty_track_info_keeps_the_measurement_keys():
    info = empty_track_info("JRA", JRATrackScraper.BABA_403)
    payload = json.loads(info.model_dump_json())
    assert payload["turf"]["cushion"] is None
    assert payload["turf"]["moisture"] is None
    assert payload["dirt"]["cushion"] is None
    assert payload["dirt"]["moisture"] is None


def _card(html: str, race_id: str) -> RaceCard:
    url = f"{NetkeibaScraper.BASE_URL}/race/shutuba.html"
    scraper = NetkeibaScraper(http_client=_Gets(gets={url: html}))  # type: ignore[arg-type]
    card = scraper.get_race_card(race_id)
    assert card is not None
    return card
