"""Operations review: odds retries and phase, race ids, encoding, pre-draw cards."""

import json
from datetime import date, datetime

import pytest
import requests
from bs4 import BeautifulSoup

from backend.cli import main as cli_main
from backend.scrapers.netkeiba_odds_scraper import (
    NetkeibaOddsScraper,
    parse_netkeiba_win_odds,
)
from backend.scrapers.netkeiba_scraper import NetkeibaScraper
from backend.scrapers.race_ids import jra_race_id, page_mismatch_warning, parse_race_id
from backend.services.race_id_service import RaceIdService
from backend.utils.date_utils import TOKYO
from backend.utils.http_client import _text_from_response
from backend.utils.text_encoding import decode_page_bytes

FIXTURES_AT = datetime(2026, 11, 1, 9, 0, tzinfo=TOKYO)
RACE_ID = "202605050811"
WIN = {
    "status": "middle",
    "data": {"official_datetime": "2026-11-01 09:00:00", "odds": {"1": {"01": ["2.1", "0", "1"]}}},
    "reason": "",
}


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


def test_race_id_help_lists_the_day_list(monkeypatch, capsys):
    import sys

    monkeypatch.setattr(sys, "argv", ["backend.cli", "race-id", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli_main()
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "2026-11-01" in text
    assert "東京" in text
    assert "403" in text
