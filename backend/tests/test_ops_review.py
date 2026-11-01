"""Ops review: full order, going freshness, trends, odds retries, and race ids."""

import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from bs4 import BeautifulSoup

from backend.cli import main as cli_main
from backend.models.odds import RaceOdds
from backend.models.race import Race, RaceCard, RaceEntry, SettledRaceResult
from backend.scrapers.day_of_bias import BiasLabels, assemble_publication
from backend.scrapers.jra_track_scraper import (
    GOING_NOT_RACE_DATE,
    JRATrackScraper,
    _weekly_cell_date,
)
from backend.scrapers.netkeiba_odds_scraper import NetkeibaOddsScraper, parse_netkeiba_win_odds
from backend.scrapers.netkeiba_scraper import NetkeibaScraper
from backend.scrapers.page_cache import CachingHTTPClient
from backend.scrapers.page_fallback import AccessBlocked, same_site_result_urls
from backend.scrapers.race_ids import jra_race_id, page_mismatch_warning, parse_race_id
from backend.scrapers.runner_status import POST_POSITIONS_MISSING, RACE_CARD_NOT_FOUND
from backend.services.historical_service import HistoricalService, _transient_failure
from backend.services.race_id_service import RaceIdService
from backend.services.race_service import RaceService
from backend.utils.http_client import _text_from_response
from backend.utils.text_encoding import decode_page_bytes

TOKYO = timezone(timedelta(hours=9))
FETCHED = datetime(2026, 11, 1, 16, 0, tzinfo=TOKYO)
DAY = date(2026, 11, 1)
RESULT_URL = "https://race.netkeiba.com/race/result.html"
FIXTURES = Path(__file__).parent / "fixtures"
FIXTURES_AT = datetime(2026, 11, 1, 9, 0, tzinfo=TOKYO)
RACE_ID = "202605050811"
WIN = {
    "status": "middle",
    "data": {"official_datetime": "2026-11-01 09:00:00", "odds": {"1": {"01": ["2.1", "0", "1"]}}},
    "reason": "",
}


def _row(rank: str, number: int, name: str, *, flash: bool = False) -> str:
    kind = "FirstDisplay HorseList" if flash else "HorseList"
    return (
        f"<tr class='{kind}'><td>{rank}</td><td>1</td><td>{number}</td>"
        f"<td><a href='https://db.netkeiba.com/horse/{number:010d}'>{name}</a></td>"
        "<td>55</td><td>騎手</td><td>1:33.0</td><td>クビ</td><td>1</td><td>2.1</td>"
        "<td>34.0</td><td>1-1</td></tr>"
    )


def _table(rows: list[str], *, show_all: bool = False) -> str:
    kind = " Table_Show_All" if show_all else ""
    body = "".join(rows)
    return (
        f"<table class='RaceTable01{kind}' summary='全着順'>"
        "<tr><th>着順</th><th>枠</th><th>馬番</th><th>馬名</th><th>斤量</th><th>騎手</th>"
        "<th>タイム</th><th>着差</th><th>人気</th><th>単勝オッズ</th><th>後3F</th>"
        f"<th>コーナー通過順</th></tr>{body}</table>"
    )


def _page(tables: str, *, heads: int, extra: str = "") -> str:
    return f"""<html><head><title>ステークス | 2026年11月1日 東京11R</title></head><body>
<div class="RaceList_Date"><a href="?kaisai_date=20261101">11月1日</a></div>
<div class="RaceList_Item02"><h1 class="RaceName">ステークス</h1>
<p class="RaceResultState">確定</p>
<div class="RaceData01"><span>芝1600m</span> / 天候:晴 <span>/ 馬場:良</span></div>
<div class="RaceData02"><span>4回</span><span>東京</span><span>8日目</span><span>{heads}頭</span></div>
</div>
{tables}
{extra}
</body></html>"""


class _Gets:
    def __init__(self, routes: dict[str, str] | None = None):
        self.routes = routes or {}
        self.calls: list[str] = []

    def get(self, url, params=None, headers=None, encoding=None):
        self.calls.append(url)
        if url in self.routes:
            return self.routes[url]
        raise AssertionError(url)

    def post(self, url, data=None, headers=None, encoding=None):
        raise AssertionError(url)

    def close(self) -> None:
        return None


def _parse(html: str, routes: dict[str, str] | None = None) -> SettledRaceResult:
    client = _Gets({RESULT_URL: html, **(routes or {})})
    scraper = NetkeibaScraper(http_client=client, headless_render="off")  # type: ignore[arg-type]
    return scraper.get_settled_race_result("202605040711", fetched_at=FETCHED)


def test_full_table_wins_over_a_leading_top5_block():
    short = _table([_row(str(n), n, f"速{n}", flash=True) for n in range(1, 6)])
    full = _table(
        [_row(str(n), n, f"馬{n}") for n in range(1, 9)],
        show_all=True,
    )
    settled = _parse(_page(short + full, heads=8))
    assert settled.complete is True
    assert [row.horse_name for row in settled.results] == [f"馬{n}" for n in range(1, 9)]
    assert settled.results[0].finish_time == "1:33.0"
    assert settled.results[0].passing_order == "1-1"
    assert settled.results[0].last_3f == 34.0
    assert settled.results[0].popularity == 1
    assert settled.results[0].odds == 2.1


def test_show_all_class_on_a_five_row_flash_stays_incomplete(tmp_path):
    html = _page(
        _table([_row(str(n), n, f"馬{n}", flash=True) for n in range(1, 6)], show_all=True),
        heads=5,
        extra="<p>5着まで表示 全着順を見る</p>",
    )
    settled = _parse(html)
    assert settled.complete is False
    assert len(settled.results) == 5
    assert "early result" in (settled.warning or "")

    calls: list[str] = []

    class _Transport:
        def get(self, url, params=None, headers=None, encoding=None):
            calls.append(url)
            return html

    url = RESULT_URL
    client = CachingHTTPClient(tmp_path, transport=_Transport(), min_request_interval=0)
    client.get(url, params={"race_id": "202605040711"})
    client.get(url, params={"race_id": "202605040711"})
    assert calls == [url, url]


def test_get_call_to_the_result_api_is_followed():
    endpoint = "https://race.netkeiba.com/api/api_get_race_result.html?race_id=202605040711"
    html = _page(
        _table([_row(str(n), n, f"馬{n}", flash=True) for n in range(1, 6)]),
        heads=8,
        extra=f"<script>$.get('{endpoint}');</script>",
    )
    assert same_site_result_urls(html, RESULT_URL + "?race_id=202605040711") == [endpoint]
    payload = {
        "result_phase": "確定",
        "entry_count": 8,
        "results": [
            {
                "rank": n,
                "horse_number": n,
                "horse_name": f"馬{n}",
                "finish_time": "1:33.0",
                "margin": "クビ",
                "popularity": 1,
                "odds": 2.1,
                "last_3f": 34.0,
                "passing_order": "1-1",
            }
            for n in range(1, 9)
        ],
    }
    import json

    settled = _parse(html, {endpoint: json.dumps(payload)})
    assert settled.complete is True
    assert len(settled.results) == 8
    assert settled.results[5].source == "netkeiba_js"


def test_latest_cushion_unit_wins_and_an_older_going_is_flagged():
    page = """
    <html><head><title>馬場情報（東京競馬場）　JRA</title></head><body>
    <p>2026年11月1日（日曜） 東京競馬場</p>
    <h2>馬場状態</h2><p>10月30日（金曜）正午現在</p><p>芝：良</p>
    <h2>クッション値</h2>
    <div class="content" id="cushion_num"></div>
    <script src="/keiba/baba/_js/baba2025.js"></script>
    </body></html>
    """
    script = "url: '_data_cushion.html',\nurl: '_data_moist.html',\n"
    cushion = """
    <div id="rcA" title="東京">
      <div class="unit"><div class="time">10月30日（金曜）9時00分</div><div class="cushion">9.1</div></div>
      <div class="unit"><div class="time">11月1日（日曜）7時00分</div><div class="cushion">9.7</div></div>
    </div>
    """
    moist = """
    <div id="rcA" title="東京">
      <div class="unit">
        <div class="time">10月30日（金曜）8時30分</div>
        <div class="turf"><span class="mg">15.3</span><span class="m4c">13.4</span></div>
        <div class="dirt"><span class="mg">7.5</span><span class="m4c">6.9</span></div>
      </div>
      <div class="unit">
        <div class="time">11月1日（日曜）5時30分</div>
        <div class="turf"><span class="mg">13.8</span><span class="m4c">13.4</span></div>
        <div class="dirt"><span class="mg">4.3</span><span class="m4c">4.3</span></div>
      </div>
    </div>
    """
    baba = "https://www.jra.go.jp/keiba/baba/index.html"
    other = "https://www.jra.go.jp/keiba/baba/index2.html"
    script_url = "https://www.jra.go.jp/keiba/baba/_js/baba2025.js"
    client = _Gets(
        {
            baba: page,
            other: "<html><head><title>馬場情報（京都競馬場）　JRA</title></head></html>",
            script_url: script,
            "https://www.jra.go.jp/keiba/baba/_data_cushion.html": cushion,
            "https://www.jra.go.jp/keiba/baba/_data_moist.html": moist,
        }
    )
    info = JRATrackScraper(http_client=client, headless_render="off").scrape(  # type: ignore[arg-type]
        "tokyo",
        fetched_at=FETCHED,
        race_date=DAY,
        today=DAY,
    )
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.value == 9.7
    assert info.turf.cushion.measured_at == datetime(2026, 11, 1, 7, 0, tzinfo=TOKYO)
    assert info.turf.moisture is not None
    assert info.turf.moisture.goal_front == 13.8
    assert info.turf.moisture.measured_at == datetime(2026, 11, 1, 5, 30, tzinfo=TOKYO)
    assert info.stale is False
    assert info.warning is not None
    assert f"{GOING_NOT_RACE_DATE}: 2026-10-30" in info.warning


def test_accessd_403_sets_blocked_reason_and_does_not_invent_a_label():
    blocked = assemble_publication(BiasLabels(), "403", BiasLabels(), "403")
    assert blocked.inside_outside is None
    assert blocked.pace_bias is None
    assert blocked.blocked_reason == "http_403"
    assert blocked.warning is not None
    assert "403" in blocked.warning

    published = assemble_publication(
        BiasLabels(),
        "403",
        BiasLabels(inside_outside="内有利", pace_bias="先行有利"),
        "ok",
    )
    assert published.blocked_reason is None
    assert published.inside_outside == "内有利"
    assert published.source == "jra_baba"


def test_transient_failures_retry_and_a_403_does_not():
    assert _transient_failure(requests.Timeout("timed out")) is True
    assert _transient_failure(requests.ConnectionError("connection reset by peer")) is True
    denied = requests.HTTPError("403")
    response = requests.Response()
    response.status_code = 403
    denied.response = response
    assert _transient_failure(denied) is False
    assert _transient_failure(AccessBlocked(403, "returned 403")) is False


def test_time_budget_returns_the_races_already_read(monkeypatch: pytest.MonkeyPatch):
    day = date(2026, 10, 18)
    seen: list[str] = []

    class _Net:
        def get_races_by_date(self, target):
            assert target == day
            return [
                Race(
                    race_id=race_id,
                    race_name="テスト",
                    race_number=number,
                    race_date=day,
                    venue="東京",
                    distance=1600,
                    track_type="芝",
                )
                for number, race_id in enumerate(("202605040701", "202605040702"), start=1)
            ]

        def get_settled_race_result(self, race_id: str, fetched_at=None):
            seen.append(race_id)
            raise requests.Timeout("timed out")

        def close(self) -> None:
            return None

    class _Quiet:
        def close(self) -> None:
            return None

    monkeypatch.setattr(time, "monotonic", lambda: 0.0 if not seen else 100.0)
    service = HistoricalService(
        netkeiba=_Net(),  # type: ignore[arg-type]
        jra=_Quiet(),  # type: ignore[arg-type]
        odds=_Quiet(),  # type: ignore[arg-type]
        delay_seconds=0,
        sleeper=lambda _seconds: None,
    )
    batch = service.get(on_date=day, time_budget=10, retries=0)
    assert seen == ["202605040701"]
    assert [race.race_id for race in batch.races] == ["202605040701"]
    assert batch.remaining_race_ids == ["202605040702"]
    assert batch.stopped_at_race_id is None
    assert batch.warning is not None
    assert "trend time budget exhausted after 10s" in batch.warning


def test_cli_trends_help_lists_the_budget_options():
    import subprocess

    completed = subprocess.run(
        ["python3", "-m", "backend.cli", "trends", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    help_text = completed.stdout
    for flag in ("--timeout", "--retries", "--concurrency", "--time-budget", "20261018"):
        assert flag in help_text


def _http_error(status: int, body: str) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    response._content = body.encode("utf-8")
    response.encoding = "utf-8"
    return requests.HTTPError(f"http {status}", response=response)


class _Scripted:
    def __init__(self, results: list[object]):
        self.results = results
        self.calls = 0

    def get(self, url, params=None, headers=None, encoding=None):
        self.calls += 1
        item = self.results[min(self.calls, len(self.results)) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        return None


def _scraper(client: _Scripted) -> NetkeibaOddsScraper:
    return NetkeibaOddsScraper(http_client=client, sleeper=lambda _seconds: None)  # type: ignore[arg-type]


def test_transient_400_is_retried_and_a_later_body_is_kept():
    client = _Scripted([_http_error(400, "<html>bad</html>"), json.dumps(WIN)])
    snapshot = _scraper(client).get_win_odds(RACE_ID, fetched_at=FIXTURES_AT)

    assert client.calls == 2
    assert snapshot.quotes[0].odds == 2.1
    assert snapshot.odds_phase == "live"
    assert snapshot.is_official_market is True
    assert snapshot.race_phase == "pre"
    assert snapshot.status == "live"


def test_decided_400_is_not_retried_and_prices_stay_out():
    body = json.dumps(
        {
            "status": "result",
            "reason": "result odds empty",
            "data": {"odds": {"1": {"01": ["99.9", "0", "1"]}}},
        }
    )
    client = _Scripted([_http_error(400, body)])
    snapshot = _scraper(client).get_win_odds(RACE_ID, fetched_at=FIXTURES_AT)

    assert client.calls == 1
    assert snapshot.quotes == []
    assert snapshot.reason == "http 400: result odds empty"
    assert snapshot.odds_phase == "final"
    assert snapshot.is_official_market is True
    assert "99.9" not in json.dumps(snapshot.model_dump(mode="json"))


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, "login"),
        (403, "<html><head><title>403 Forbidden</title></head><body>Access Denied</body></html>"),
        (
            400,
            "<html><head><title>Just a moment...</title></head><body>" + ("wait " * 40) + "</body></html>",
        ),
        (500, "<html><head><title>CAPTCHA認証</title></head><body>" + ("wait " * 20) + "</body></html>"),
    ],
)
def test_refusal_is_not_retried(status: int, body: str):
    client = _Scripted([_http_error(status, body), json.dumps(WIN)])
    snapshot = _scraper(client).get_win_odds(RACE_ID, fetched_at=FIXTURES_AT)

    assert client.calls == 1
    assert snapshot.quotes == []


def test_connection_error_retries_then_stays_empty():
    client = _Scripted([requests.ConnectionError("down")] * 3)
    snapshot = _scraper(client).get_win_odds(RACE_ID, fetched_at=FIXTURES_AT)

    assert client.calls == 3
    assert snapshot.quotes == []
    assert snapshot.reason == "fetch failed"
    assert snapshot.odds_phase is None
    assert snapshot.is_official_market is None


def test_sale_phases_follow_the_api_and_do_not_relabel_kind():
    yoso = dict(WIN)
    yoso["status"] = "yoso"
    forecast = parse_netkeiba_win_odds(yoso, RACE_ID, fetched_at=FIXTURES_AT)
    assert forecast.odds_phase == "forecast"
    assert forecast.is_official_market is False
    assert forecast.status == "yoso"
    assert forecast.quotes[0].kind == "yoso"
    assert forecast.race_phase == "pre"

    provisional = dict(WIN)
    provisional["reason"] = "前日暫定"
    board = parse_netkeiba_win_odds(provisional, RACE_ID, fetched_at=FIXTURES_AT)
    assert board.status == "live"
    assert board.quotes[0].kind == "live"
    assert board.odds_phase == "provisional"
    assert board.is_official_market is False

    final = dict(WIN)
    final["status"] = "result"
    settled = parse_netkeiba_win_odds(final, RACE_ID, fetched_at=FIXTURES_AT)
    assert settled.odds_phase == "final"
    assert settled.is_official_market is True
    assert settled.race_phase == "post"
    assert settled.quotes[0].kind == "live"


def test_euc_jp_bytes_decode_when_utf8_was_requested():
    html = (
        "<html><head><meta charset='euc-jp'></head><body>"
        "<a href='/horse/2019104308'>エフォートレス</a></body></html>"
    ).encode("euc_jp")
    text = decode_page_bytes(html, requested="utf-8", content_type="text/html")
    assert "エフォートレス" in text
    assert "\ufffd" not in text

    class _Response:
        content = html
        headers = {"Content-Type": "text/html; charset=EUC-JP"}
        encoding = "utf-8"

    assert "エフォートレス" in _text_from_response(_Response(), "utf-8")  # type: ignore[arg-type]


class _ListClient:
    def __init__(self, pages: dict[str, str], errors: dict[str, Exception] | None = None):
        self.pages = pages
        self.errors = errors or {}
        self.calls: list[str] = []

    def get(self, url, params=None, headers=None, encoding=None):
        self.calls.append(url)
        if url in self.errors:
            raise self.errors[url]
        return self.pages.get(url, "")

    def close(self):
        return None


def _list_html() -> str:
    return """
    <dl>
      <dt><p class="RaceList_DataTitle"><small>5回</small> 東京 <small>2日目</small></p></dt>
      <dd><ul>
        <li class="RaceList_DataItem">
          <a href="../race/shutuba.html?race_id=202605050202">
            <span class="ItemTitle">2歳未勝利</span>
            <div class="RaceData"><span>芝1600m</span><span class="RaceList_Itemnumber">16頭</span></div>
          </a>
        </li>
        <li class="RaceList_DataItem">
          <a href="../race/shutuba.html?race_id=202605050211">
            <span class="ItemTitle">アルテミスステークス</span>
            <div class="RaceData"><span>芝1600m</span><span class="RaceList_Itemnumber">16頭</span></div>
          </a>
        </li>
      </ul></dd>
    </dl>
    """


def test_race_id_comes_from_the_day_list_not_a_guessed_kai():
    client = _ListClient({"https://race.netkeiba.com/top/race_list.html": _list_html()})
    scraper = NetkeibaScraper(http_client=client, headless_render="off")  # type: ignore[arg-type]
    listed = RaceIdService(scraper).resolve(date(2026, 11, 1), "東京", 11, fetched_at=FIXTURES_AT)

    assert listed.warning is None
    assert len(listed.races) == 1
    row = listed.races[0]
    assert row.netkeiba_race_id == "202605050211"
    assert row.jra_race_id == "202611010511"
    assert row.round_number == 5
    assert row.day_number == 2
    assert row.venue == "東京"
    assert row.source == "netkeiba_html"
    assert row.fetched_at == FIXTURES_AT
    assert row.fetched_at.utcoffset() is not None
    assert parse_race_id("202605050811").kai == 5
    assert jra_race_id(date(2026, 11, 1), "05", 11) == "202611010511"
    assert client.calls == ["https://race.netkeiba.com/top/race_list.html"]


def test_race_list_403_is_not_retried_and_ids_stay_null():
    error = _http_error(403, "<html><title>403 Forbidden</title><h1>Access Denied</h1></html>")
    client = _ListClient({}, {"https://race.netkeiba.com/top/race_list.html": error})
    scraper = NetkeibaScraper(http_client=client, headless_render="off")  # type: ignore[arg-type]
    listed = RaceIdService(scraper).resolve(date(2026, 11, 1), "東京", 11, fetched_at=FIXTURES_AT)

    assert client.calls == ["https://race.netkeiba.com/top/race_list.html"]
    assert listed.races == []
    assert listed.warning == "race list returned 403"
    assert listed.source is None


def test_page_mismatch_warns_and_does_not_rewrite_the_id():
    assert page_mismatch_warning(
        "202605050811",
        venue="京都",
        round_number=5,
        day_number=8,
    ) == "race id does not match the page: venue id=東京 page=京都"
    assert page_mismatch_warning("202605050811", venue="東京", round_number=5, day_number=8) is None
    assert parse_race_id("not-a-race").kind is None


def test_card_before_post_positions_keeps_horses_with_null_numbers():
    html = """
    <html><body>
      <h1 class="RaceName">アルテミスS</h1>
      <div class="RaceData02"><span>5回</span><span>東京</span><span>8日目</span></div>
      <table class="Shutuba_Table">
        <tr><th>馬名</th><th>性齢</th><th>騎手</th></tr>
        <tr>
          <td><a href="/horse/2019104308">エフォートレス</a></td>
          <td>牡4</td>
          <td>ルメール</td>
        </tr>
        <tr>
          <td><a href="/horse/2020104308">サウンドムーブ</a></td>
          <td>牝3</td>
          <td>川田</td>
        </tr>
      </table>
    </body></html>
    """
    client = _ListClient({"https://race.netkeiba.com/race/shutuba.html": html})
    scraper = NetkeibaScraper(http_client=client, headless_render="off")  # type: ignore[arg-type]
    card = scraper.get_race_card("202605050811")

    assert card is not None
    assert [entry.horse_name for entry in card.entries] == ["エフォートレス", "サウンドムーブ"]
    assert all(entry.bracket_number is None and entry.horse_number is None for entry in card.entries)
    assert card.entries[0].status == "entered"
    assert card.warning == "post positions were not on the page"
    assert card.race.venue == "東京"
    assert card.race.round_number == 5
    assert card.race.day_number == 8


def test_euc_jp_card_names_are_unicode():
    html = """
    <html><head><meta http-equiv="Content-Type" content="text/html; charset=euc-jp"></head>
    <body>
      <div class="RaceData02"><span>5回</span><span>東京</span><span>2日目</span></div>
      <table class="Shutuba_Table RaceTable">
        <tr><td>1</td><td>1</td><td></td><td><a href="/horse/2019104308">エフォートレス</a></td><td>牡4</td></tr>
      </table>
    </body></html>
    """.encode("euc_jp")
    text = decode_page_bytes(html, requested="utf-8")
    soup = BeautifulSoup(text, "lxml")
    scraper = NetkeibaScraper(headless_render="off")
    entries = scraper._parse_race_entries(soup, source="netkeiba_html", fetched_at=FIXTURES_AT)
    assert entries[0].horse_name == "エフォートレス"
    assert entries[0].bracket_number == 1
    assert entries[0].horse_number == 1


def test_real_waku_classes_keep_seventeen_numbered_rows():
    html = (FIXTURES / "netkeiba_shutuba_waku_classes.html").read_text(encoding="utf-8")
    client = _ListClient({"https://race.netkeiba.com/race/shutuba.html": html})
    scraper = NetkeibaScraper(http_client=client, headless_render="off")  # type: ignore[arg-type]
    card = scraper.get_race_card("202608050811")

    assert card is not None
    assert len(card.entries) == 17
    assert [entry.horse_number for entry in card.entries] == list(range(1, 18))
    assert all(entry.bracket_number is not None for entry in card.entries)
    assert card.entries[12].horse_name == "馬13"
    assert card.entries[12].horse_number == 13
    assert card.warning is None


def _named(name: str, number: int | None, bracket: int | None = None) -> RaceEntry:
    return RaceEntry(
        horse_name=name,
        horse_number=number,
        bracket_number=bracket,
        status="entered",
    )


def test_merge_matches_a_null_number_by_horse_name():
    service = RaceService()
    primary = [
        _named("エフォートレス", None),
        _named("サウンド ムーブ", None),
    ]
    secondary = [
        _named("サウンドムーブ", 2, 2),
        _named("エフォートレス", 1, 1),
    ]
    merged = service._merge_entries(primary, secondary)

    assert [(entry.horse_name, entry.horse_number) for entry in merged] == [
        ("エフォートレス", 1),
        ("サウンド ムーブ", 2),
    ]
    assert len(merged) == 2


def test_merge_drops_the_post_position_warning_once_numbers_are_filled():
    race = Race(
        race_id="202608050811",
        race_name="京都11R",
        race_number=11,
        race_date=date(2026, 11, 1),
        venue="京都",
        distance=1600,
        track_type="芝",
    )
    service = RaceService()
    netkeiba = RaceCard(
        race=race,
        entries=[_named("馬01", None), _named("馬02", None)],
        warning=POST_POSITIONS_MISSING,
    )
    jra = RaceCard(
        race=race,
        entries=[_named("馬01", 1, 1), _named("馬02", 2, 1)],
    )
    card = service._merge_race_cards(netkeiba, jra)

    assert card is not None
    assert len(card.entries) == 2
    assert [entry.horse_number for entry in card.entries] == [1, 2]
    assert card.warning is None


def test_invalid_date_is_a_warning_and_json_is_only_json(monkeypatch, capsys):
    snapshot = RaceOdds(
        race_id=RACE_ID,
        bet_types=["win"],
        prefer="live",
        status="unavailable",
        fetched_at=FIXTURES_AT,
    )

    class _Service:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get_race_odds(self, race_id, bet_types="win", prefer="live", **kwargs):
            assert "expect_date" not in kwargs
            return snapshot

    monkeypatch.setattr("backend.cli.RaceService", lambda: _Service())
    monkeypatch.setattr(
        sys,
        "argv",
        ["backend.cli", "odds", RACE_ID, "--json", "--date", "2026-11-31"],
    )
    cli_main()
    out = capsys.readouterr().out
    assert out.strip().startswith("{")
    payload = json.loads(out)
    assert payload["warning"] == "track race date was not a date"
    assert payload["quotes"] == []


def test_card_json_stdout_is_only_json(monkeypatch, capsys):
    race = Race(
        race_id=RACE_ID,
        race_name="京都11R",
        race_number=11,
        race_date=date(2026, 11, 1),
        venue="京都",
        distance=1600,
        track_type="芝",
    )
    card = RaceCard(race=race, entries=[_named("馬01", 1, 1)])

    class _Service:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get_race_card(self, race_id, **kwargs):
            return card

    monkeypatch.setattr("backend.cli.RaceService", lambda: _Service())
    monkeypatch.setattr(sys, "argv", ["backend.cli", "card", RACE_ID, "--json"])
    cli_main()
    out = capsys.readouterr().out
    assert "出馬表" not in out
    assert json.loads(out)["entries"][0]["horse_number"] == 1


def test_http_429_is_not_retried_and_warns():
    client = _Scripted([_http_error(429, "slow down"), json.dumps(WIN)])
    snapshot = _scraper(client).get_win_odds(RACE_ID, fetched_at=FIXTURES_AT)

    assert client.calls == 1
    assert snapshot.quotes == []
    assert snapshot.reason == "http 429"
    assert snapshot.warning == "netkeiba odds returned 429"


def test_http_408_is_retried():
    client = _Scripted([_http_error(408, ""), json.dumps(WIN)])
    snapshot = _scraper(client).get_win_odds(RACE_ID, fetched_at=FIXTURES_AT)

    assert client.calls == 2
    assert snapshot.quotes[0].odds == 2.1


def test_weekly_cell_uses_the_previous_month_and_the_year_boundary():
    november = datetime(2026, 11, 1)
    assert _weekly_cell_date("31日（土）", november) == datetime(2026, 10, 31)
    assert _weekly_cell_date("10月31日（土）", november) == datetime(2026, 10, 31)
    assert _weekly_cell_date("1日（日）", november) == datetime(2026, 11, 1)
    january = datetime(2027, 1, 3)
    assert _weekly_cell_date("31日", january) == datetime(2027, 1, 31)
    assert _weekly_cell_date("12月28日（月）", january) == datetime(2026, 12, 28)
    february = datetime(2027, 2, 1)
    assert _weekly_cell_date("31日", february) == datetime(2027, 1, 31)
    assert _weekly_cell_date("30日", february) == datetime(2027, 1, 30)
    december = datetime(2026, 12, 28)
    assert _weekly_cell_date("1月2日（土）", december) == datetime(2027, 1, 2)
    assert _weekly_cell_date("32日", november) is None
    assert _weekly_cell_date("2月31日", november) is None


def test_empty_card_warns_that_it_was_not_found():
    html = "<html><head><title>出馬表</title></head><body></body></html>"

    class _Client:
        def get(self, url, params=None, headers=None, encoding=None):
            return html

        def post(self, url, data=None, headers=None, encoding=None):
            raise AssertionError(url)

        def close(self) -> None:
            return None

    card = NetkeibaScraper(http_client=_Client(), headless_render="off").get_race_card(  # type: ignore[arg-type]
        RACE_ID
    )
    assert card is not None
    assert card.entries == []
    assert card.race.race_name == f"Race {RACE_ID}"
    assert card.race.venue == "不明"
    assert card.warning == RACE_CARD_NOT_FOUND


def test_item03_going_without_a_label_is_read():
    html = """
    <div class="RaceData01"><span>芝2500m</span> /
      <span class="Item03">良</span>
    </div>
    <div class="RaceData02"><span>中山</span></div>
    """
    header = NetkeibaScraper(headless_render="off")._settled_header(BeautifulSoup(html, "lxml"))
    assert header.track_condition == "良"
    assert header.distance == 2500
    assert header.track_type == "芝"


def test_race_id_help_lists_the_day_list(monkeypatch, capsys):

    monkeypatch.setattr(sys, "argv", ["backend.cli", "race-id", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli_main()
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "2026-11-01" in text
    assert "東京" in text
    assert "403" in text
