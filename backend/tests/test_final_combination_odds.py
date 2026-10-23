"""Past final wide, umaren, and sanrenpuku odds.

A price is final only for a confirmed complete race. 最終オッズ is that label.
The winning ticket is checked against the payout. A missing market stays null.
"""

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests
from fastapi.testclient import TestClient

from backend.cli import main as cli_main
from backend.main import app
from backend.models.odds import FinalRaceOdds, OddsQuote
from backend.scrapers.final_odds import (
    PublicOddsPages,
    final_odds_need_fallback,
    odds_api_is_final_payload,
    odds_api_is_replayable,
    page_says_final_odds,
    parse_confirmed_odds,
    payout_matches_quote,
    quotes_from_result_api,
)
from backend.scrapers.netkeiba_scraper import NetkeibaScraper
from backend.scrapers.page_cache import CachingHTTPClient
from backend.scrapers.page_fallback import (
    HEADLESS_UNAVAILABLE,
    AccessBlocked,
    FetchedBody,
    HeadlessUnavailableError,
    headless_browser_installed,
    render_headless,
)
from backend.services.final_odds_service import FinalOddsService
from backend.services.race_service import RaceService

FIXTURES = Path(__file__).parent / "fixtures"
RACE_ID = "202502020101"
TOKYO = ZoneInfo("Asia/Tokyo")
FETCHED = datetime(2026, 10, 12, 9, 0, tzinfo=TOKYO)
_CLOCK = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")


def _prices(snapshot: FinalRaceOdds) -> str:
    """JSON with clocks removed, so a timestamp like 41.140 cannot look like odds 1.1."""
    return _CLOCK.sub("", snapshot.model_dump_json())


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _page(body: str, source: str = "jra_html") -> FetchedBody:
    return FetchedBody(
        body=body,
        source=source,
        fetched_at=FETCHED,
        url="https://www.jra.go.jp/odds",
    )


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"http {status}", response=response)


def _runner(rank: str, number: int, name: str) -> str:
    return (
        "<tr class='HorseList'>"
        f"<td class='Result_Num'><div class='Rank'>{rank}</div></td>"
        f"<td><div>1</div></td><td><div>{number}</div></td>"
        f"<td class='Horse_Info'><a href='https://db.netkeiba.com/horse/{number:010d}'>{name}</a></td>"
        "<td><span class='JockeyWeight'>55.0</span></td><td>騎手</td><td>1:10.0</td><td></td>"
        f"<td>1</td><td>3.4</td><td></td><td></td></tr>"
    )


def _payout_row(
    label: str, groups: list[tuple[str, ...]], amounts: list[int], pops: list[int]
) -> str:
    combos = "".join("<ul>" + "".join(f"<li>{n}</li>" for n in group) + "</ul>" for group in groups)
    yen = "".join(f"<span>{amount}円</span>" for amount in amounts)
    ninki = "".join(f"<span>{pop}人気</span>" for pop in pops)
    return (
        f"<tr><th>{label}</th><td class='Result'>{combos}</td>"
        f"<td class='Payout'>{yen}</td><td class='Ninki'>{ninki}</td></tr>"
    )


def _matching_payouts(umaren_yen: int = 1240) -> str:
    return "".join(
        [
            _payout_row("ワイド", [("1", "3"), ("1", "5"), ("3", "5")], [280, 500, 610], [2, 8, 9]),
            _payout_row("馬連", [("1", "3")], [umaren_yen], [4]),
            _payout_row("3連複", [("1", "3", "5")], [4060], [6]),
        ]
    )


def _result_html(
    *,
    phase: str = "確定",
    heads: int = 4,
    rows: str | None = None,
    payouts: str | None = None,
) -> str:
    body_rows = rows or "".join(
        [
            _runner("1", 1, "アルファ"),
            _runner("2", 3, "ベータ"),
            _runner("3", 5, "ガンマ"),
            _runner("取消", 2, "デルタ"),
        ]
    )
    money = _matching_payouts() if payouts is None else payouts
    return f"""<!DOCTYPE html><html><head><title>テスト | 2025年2月2日 東京1R</title></head><body>
<div class="RaceList_Date"><dl id="RaceList_DateList"><dd class="Active">
<a href="../top/race_list.html?kaisai_date=20250202">2月2日</a></dd></dl></div>
<div class="RaceList_Item02">
<h1 class="RaceName">平地戦</h1>
<p class="RaceResultState">{phase}</p>
<div class="RaceData01"><span>芝1600m</span> / 天候:晴 <span class="Item03">/ 馬場:良</span></div>
<div class="RaceData02"><span>1回</span><span>東京</span><span>2日目</span>
<span>オープン</span><span>{heads}頭</span></div>
</div>
<table summary="全着順"><tr>
<th>着順</th><th>枠</th><th>馬番</th><th>馬名</th><th>斤量</th><th>騎手</th>
<th>タイム</th><th>着差</th><th>人気</th><th>単勝オッズ</th><th>後3F</th><th>コーナー通過順</th>
</tr>
{body_rows}
</table>
<table class="Payout_Detail_Table">{money}</table>
</body></html>"""


def _wide_only_html() -> str:
    return """<!DOCTYPE html><html><head><title>最終オッズ</title></head><body>
<h1>最終オッズ</h1>
<table id="wide">
<tr data-combination="1-3" data-popularity="2"><td>1-3</td><td class="odds">2.2-3.5</td></tr>
</table>
</body></html>"""


def _shell(api_type: str) -> str:
    url = f"https://race.netkeiba.com/api/api_get_jra_odds.html?race_id={RACE_ID}&type={api_type}"
    return (
        "<!DOCTYPE html><html><head><title>オッズ</title></head><body>"
        f"<script>var config = {{ url: '{url}' }};</script>"
        "<p>読み込み中</p></body></html>"
    )


def _api_body(key: str, combos: dict[str, list[str]], official: str = "2025-02-02 15:40:00") -> str:
    return json.dumps(
        {
            "status": "result",
            "data": {"official_datetime": official, "odds": {key: combos}},
            "reason": "",
        },
        ensure_ascii=False,
    )


def _query_type(url: str) -> str:
    values = parse_qs(urlparse(url).query).get("type") or []
    return values[0] if values else ""


class _Router:
    """Result HTML, then an odds page or the API that page names."""

    def __init__(
        self,
        result_html: str,
        *,
        odds_html: str | None = None,
        api: dict[str, str] | None = None,
        result_error: Exception | None = None,
        odds_error: Exception | None = None,
    ):
        self.result_html = result_html
        self.odds_html = odds_html
        self.api = api or {}
        self.result_error = result_error
        self.odds_error = odds_error
        self.gets: list[str] = []
        self.params: list[dict | None] = []

    def get(self, url, params=None, headers=None, encoding=None):
        self.gets.append(url)
        self.params.append(params)
        if "result.html" in url:
            if self.result_error is not None:
                raise self.result_error
            return self.result_html
        if "api_get_jra_odds.html" in url:
            kind = _query_type(url) or str((params or {}).get("type") or "")
            if kind not in self.api:
                return '{"status":"middle","data":""}'
            return self.api[kind]
        if "odds/index.html" in url:
            if self.odds_error is not None:
                raise self.odds_error
            if self.odds_html is not None:
                return self.odds_html
            kind = str((params or {}).get("type") or "")
            return _shell({"b5": "5", "b4": "4", "b7": "7"}[kind])
        raise AssertionError(url)

    def post(self, url, data=None, headers=None, encoding=None):
        raise requests.RequestException("jra payout unreachable")

    def close(self) -> None:
        return None


class _Jra:
    def __init__(self, pages: PublicOddsPages):
        self.pages = pages
        self.calls: list[str] = []

    def fetch_odds_pages(self, race_id, fetched_at=None):
        self.calls.append(race_id)
        return self.pages

    def close(self) -> None:
        return None


class _Headless:
    def __init__(self, html: str):
        self.html = html
        self.calls: list[str] = []

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        return self.html


def _service(
    result_html: str,
    jra: PublicOddsPages,
    *,
    http: _Router | CachingHTTPClient | None = None,
    headless_render: object = "off",
    result_error: Exception | None = None,
    odds_error: Exception | None = None,
    odds_html: str | None = None,
    api: dict[str, str] | None = None,
) -> tuple[FinalOddsService, _Router | CachingHTTPClient, _Jra]:
    client = http or _Router(
        result_html,
        odds_html=odds_html,
        api=api,
        result_error=result_error,
        odds_error=odds_error,
    )
    netkeiba = NetkeibaScraper(http_client=client, headless_render="off")  # type: ignore[arg-type]
    jra_pages = _Jra(jra)
    service = FinalOddsService(
        netkeiba=netkeiba,
        jra=jra_pages,  # type: ignore[arg-type]
        http=client,  # type: ignore[arg-type]
        headless_render=headless_render,  # type: ignore[arg-type]
    )
    return service, client, jra_pages


def _by_key(quotes: list[OddsQuote]) -> dict[tuple[str, str], OddsQuote]:
    return {(quote.bet_type, quote.combination): quote for quote in quotes}


def _assert_final_clock(snapshot: FinalRaceOdds) -> None:
    assert snapshot.encoding == "utf-8"
    assert snapshot.fetched_at.utcoffset() == timedelta(hours=9)
    assert snapshot.fetched_at.isoformat().endswith("+09:00")
    for quote in snapshot.quotes:
        assert quote.fetched_at.utcoffset() == timedelta(hours=9)


def test_last_odds_label_is_final_and_keeps_popularity():
    assert page_says_final_odds("最終オッズ") is True
    assert page_says_final_odds("確 定 オッズ") is True
    assert page_says_final_odds("結果確定") is False
    menu = (
        "<html><body><nav><a>最終オッズ</a></nav><button>最終オッズ</button>"
        "<h1><a>最終オッズ</a></h1>"
        "<table><tr data-combination='1-3'><td class='odds'>1.1</td></tr></table>"
        "</body></html>"
    )
    assert page_says_final_odds(menu) is False
    assert parse_confirmed_odds([_page(menu)], RACE_ID).quotes == []
    heading = "<html><head><title>レース</title></head><body><h1>最終オッズ</h1></body></html>"
    assert page_says_final_odds(heading) is True
    assert page_says_final_odds("<table><tr><th>最終オッズ</th></tr></table>") is True

    parsed = parse_confirmed_odds([_page(_load("jra_last_odds.html"))], RACE_ID)
    assert parsed.confirmed is True
    by_key = _by_key(parsed.quotes)
    umaren = by_key[("umaren", "1-3")]
    assert umaren.odds == 12.4
    assert umaren.popularity == 4
    assert umaren.source == "jra_html"
    withheld = by_key[("umaren", "1-2")]
    assert withheld.odds is None
    assert withheld.withheld is True
    wide = by_key[("wide", "1-3")]
    assert wide.odds is None
    assert wide.odds_min == 2.2
    assert wide.odds_max == 3.5
    assert wide.popularity == 2
    assert by_key[("wide", "1-5")].odds_min == 4.0
    assert by_key[("wide", "3-5")].odds_max == 7.2
    assert by_key[("sanrenpuku", "1-3-5")].odds == 40.6
    assert by_key[("sanrenpuku", "1-3-5")].popularity == 6
    assert payout_matches_quote(1240, umaren) is True
    assert payout_matches_quote(1250, umaren) is True
    assert payout_matches_quote(1251, umaren) is False
    assert payout_matches_quote(280, wide) is True
    assert payout_matches_quote(209, wide) is False


def test_live_page_without_the_label_is_not_final():
    parsed = parse_confirmed_odds([_page(_load("jra_live_odds.html"))], RACE_ID)
    assert parsed.confirmed is False
    assert parsed.quotes == []


def test_confirmed_race_returns_final_combination_odds():
    pages = PublicOddsPages([_page(_load("jra_last_odds.html"))], None, False)
    service, client, jra = _service(_result_html(), pages)
    snapshot = service.get(RACE_ID)

    assert jra.calls == [RACE_ID]
    assert not any("odds/index.html" in url for url in client.gets)
    assert snapshot.odds_final is True
    assert snapshot.race_phase == "post"
    assert snapshot.result_phase == "confirmed"
    assert snapshot.complete is True
    assert snapshot.status == "result"
    assert snapshot.warning is None
    assert snapshot.bet_types == ["wide", "umaren", "sanrenpuku"]
    _assert_final_clock(snapshot)
    by_key = _by_key(snapshot.quotes)
    assert set(by_key) >= {("wide", "1-3"), ("umaren", "1-3"), ("sanrenpuku", "1-3-5")}
    assert by_key[("umaren", "1-3")].odds == 12.4
    assert by_key[("umaren", "1-3")].popularity == 4
    assert by_key[("wide", "1-3")].odds_min == 2.2
    assert by_key[("wide", "1-3")].odds_max == 3.5
    assert by_key[("sanrenpuku", "1-3-5")].odds == 40.6
    assert all(quote.source == "jra_html" for quote in snapshot.quotes)
    assert all(quote.bet_type != "win" for quote in snapshot.quotes)
    assert "1.1" not in _prices(snapshot)


def test_payout_disagreement_keeps_the_printed_odds():
    pages = PublicOddsPages([_page(_load("jra_last_odds.html"))], None, False)
    html = _result_html(payouts=_matching_payouts(umaren_yen=2000))
    service, _client, _jra = _service(html, pages)
    snapshot = service.get(RACE_ID, bet_types="umaren")

    assert snapshot.odds_final is True
    quote = _by_key(snapshot.quotes)[("umaren", "1-3")]
    assert quote.odds == 12.4
    assert snapshot.warning == "final odds and payout disagree: umaren 1-3"


def test_provisional_result_does_not_open_odds_pages():
    service, client, jra = _service(_result_html(phase="確定前"), PublicOddsPages([], None, False))
    snapshot = service.get(RACE_ID)

    assert jra.calls == []
    assert client.gets
    assert all("result.html" in url for url in client.gets)
    assert snapshot.quotes == []
    assert snapshot.odds_final is False
    assert snapshot.race_phase is None
    assert snapshot.result_phase == "provisional"
    assert snapshot.warning == "result phase is provisional"
    _assert_final_clock(snapshot)


def test_incomplete_result_returns_null_odds():
    rows = _runner("1", 1, "アルファ") + _runner("2", 3, "ベータ")
    service, _client, jra = _service(
        _result_html(rows=rows, heads=4),
        PublicOddsPages([], None, False),
    )
    snapshot = service.get(RACE_ID)

    assert jra.calls == []
    assert snapshot.quotes == []
    assert snapshot.odds_final is False
    assert snapshot.race_phase is None
    assert snapshot.complete is False
    assert snapshot.warning is not None
    assert "incomplete" in snapshot.warning


def test_result_403_does_not_open_odds_pages():
    service, client, jra = _service(
        _result_html(),
        PublicOddsPages([], None, False),
        result_error=_http_error(403),
    )
    snapshot = service.get(RACE_ID)

    assert jra.calls == []
    assert all("odds" not in url for url in client.gets)
    assert snapshot.quotes == []
    assert snapshot.odds_final is False
    assert snapshot.warning == "race result returned 403"


def test_live_board_is_not_copied_onto_a_confirmed_race():
    headless = _Headless(_load("jra_last_odds.html"))
    pages = PublicOddsPages([_page(_load("jra_live_odds.html"))], None, False)
    service, client, _jra = _service(
        _result_html(),
        pages,
        odds_html=_load("jra_live_odds.html"),
        headless_render=headless,
    )
    snapshot = service.get(RACE_ID)

    assert headless.calls == []
    assert any("odds/index.html" in url for url in client.gets)
    assert snapshot.quotes == []
    assert snapshot.odds_final is False
    assert snapshot.race_phase is None
    assert snapshot.result_phase == "confirmed"
    assert snapshot.complete is True
    assert snapshot.warning == ("combination odds were not on the page: wide, umaren, sanrenpuku")
    dumped = _prices(snapshot)
    assert "1.1" not in dumped
    assert "99.9" not in dumped


def test_missing_markets_stay_null_and_do_not_disagree():
    pages = PublicOddsPages([_page(_wide_only_html())], None, False)
    service, _client, _jra = _service(
        _result_html(),
        pages,
        odds_html=_load("jra_live_odds.html"),
    )
    snapshot = service.get(RACE_ID)

    assert snapshot.odds_final is True
    assert {quote.bet_type for quote in snapshot.quotes} == {"wide"}
    assert snapshot.quotes[0].odds_min == 2.2
    assert snapshot.warning == "combination odds were not on the page: umaren, sanrenpuku"
    assert "disagree" not in (snapshot.warning or "")
    assert all(quote.bet_type != "sanrenpuku" for quote in snapshot.quotes)


def test_same_site_odds_api_fills_final_combinations():
    api = {
        "5": _api_body(
            "5",
            {
                "0103": ["2.2", "3.5", "2"],
                "0105": ["4.0", "6.0", "8"],
                "0305": ["5.1", "7.2", "9"],
            },
        ),
        "4": _api_body("4", {"0103": ["12.4", "0.0", "4"], "0102": ["----", "0.0", ""]}),
        "7": _api_body("7", {"010305": ["40.6", "0.0", "6"]}),
    }
    headless = _Headless(_load("jra_last_odds.html"))
    service, client, _jra = _service(
        _result_html(),
        PublicOddsPages([], None, False),
        api=api,
        headless_render=headless,
    )
    snapshot = service.get(RACE_ID)

    assert headless.calls == []
    assert any("type=5" in url for url in client.gets)
    assert all("type=5" not in str(params) for params in client.params if params)
    by_key = _by_key(snapshot.quotes)
    assert snapshot.odds_final is True
    assert snapshot.race_phase == "post"
    assert snapshot.warning is None
    assert by_key[("wide", "1-3")].odds_min == 2.2
    assert by_key[("wide", "1-3")].popularity == 2
    assert by_key[("umaren", "1-3")].odds == 12.4
    assert by_key[("umaren", "1-3")].popularity == 4
    assert by_key[("umaren", "1-2")].withheld is True
    assert by_key[("umaren", "1-2")].popularity is None
    assert by_key[("sanrenpuku", "1-3-5")].odds == 40.6
    assert by_key[("sanrenpuku", "1-3-5")].popularity == 6
    assert all(quote.source == "netkeiba_js" for quote in snapshot.quotes)
    _assert_final_clock(snapshot)


def test_headless_renders_a_shell_and_html_mode_does_not():
    headless = _Headless(_load("jra_last_odds.html"))
    rendered, _client, _jra = _service(
        _result_html(),
        PublicOddsPages([], None, False),
        odds_html="<html><body>読み込み中</body></html>",
        headless_render=headless,
    )
    snapshot = rendered.get(RACE_ID, bet_types="wide")
    assert headless.calls
    assert snapshot.odds_final is True
    assert snapshot.quotes[0].source == "headless"
    assert snapshot.quotes[0].bet_type == "wide"

    quiet = _Headless(_load("jra_last_odds.html"))
    html_mode, _http, _pages = _service(
        _result_html(),
        PublicOddsPages([], None, False),
        odds_html="<html><body>読み込み中</body></html>",
        headless_render="off",
    )
    empty = html_mode.get(RACE_ID, bet_types="wide")
    assert quiet.calls == []
    assert empty.quotes == []
    assert empty.odds_final is False
    assert "headless browser is not installed" not in (empty.warning or "")


def test_missing_headless_browser_warns_on_final_odds(monkeypatch):
    def _missing(url: str) -> str:
        raise HeadlessUnavailableError("playwright is not installed")

    monkeypatch.setattr("backend.services.final_odds_service.render_headless", _missing)
    service, _client, _jra = _service(
        _result_html(),
        PublicOddsPages([], None, False),
        odds_html="<html><body>読み込み中</body></html>",
        headless_render="auto",
    )
    snapshot = service.get(RACE_ID, bet_types="wide")
    assert snapshot.quotes == []
    assert snapshot.odds_final is False
    assert snapshot.warning is not None
    assert HEADLESS_UNAVAILABLE in snapshot.warning


def test_netkeiba_403_stops_the_chain():
    headless = _Headless(_load("jra_last_odds.html"))
    service, client, _jra = _service(
        _result_html(),
        PublicOddsPages([], None, False),
        odds_error=_http_error(403),
        headless_render=headless,
    )
    snapshot = service.get(RACE_ID)

    assert headless.calls == []
    assert sum("odds/index.html" in url for url in client.gets) == 1
    assert snapshot.quotes == []
    assert snapshot.odds_final is False
    assert snapshot.race_phase is None
    assert snapshot.warning == "netkeiba odds returned 403"


def test_headless_block_reason_names_captcha_challenge_and_denial():
    shell = "<html><body>読み込み中</body></html>"
    cases = (
        ("captcha", None, "returned a captcha", "netkeiba returned a captcha"),
        ("challenge", None, "returned a challenge", "netkeiba returned a challenge"),
        ("http_403", None, "returned 403", "netkeiba odds returned 403"),
        ("login", 401, "requires login", "netkeiba odds requires login"),
    )
    for reason, status, message, warning in cases:
        calls: list[str] = []

        def render(
            url: str,
            *,
            _reason: str = reason,
            _status: int | None = status,
            _message: str = message,
        ) -> str:
            calls.append(url)
            raise AccessBlocked(_status, _message, reason=_reason)

        service, client, _jra = _service(
            _result_html(),
            PublicOddsPages([], None, False),
            odds_html=shell,
            headless_render=render,
        )
        snapshot = service.get(RACE_ID, bet_types="wide,umaren")
        assert snapshot.quotes == []
        assert snapshot.odds_final is False
        assert snapshot.warning == warning
        assert len(calls) == 1
        assert sum("odds/index.html" in url for url in client.gets) == 1


def test_jra_403_tries_netkeiba_once_per_missing_market():
    pages = PublicOddsPages([], "JRA odds returned 403", True)
    service, client, jra = _service(
        _result_html(),
        pages,
        odds_html=_load("jra_last_odds.html"),
    )
    snapshot = service.get(RACE_ID)

    assert jra.calls == [RACE_ID]
    assert sum("odds/index.html" in url for url in client.gets) == 3
    assert snapshot.odds_final is True
    assert all(quote.source == "netkeiba_html" for quote in snapshot.quotes)
    assert snapshot.warning == "JRA odds returned 403"
    assert _by_key(snapshot.quotes)[("umaren", "1-3")].odds == 12.4


def test_unknown_bet_type_requests_nothing():
    service, client, jra = _service(_result_html(), PublicOddsPages([], None, False))
    snapshot = service.get(RACE_ID, bet_types="win")

    assert client.gets == []
    assert jra.calls == []
    assert snapshot.quotes == []
    assert snapshot.odds_final is False
    assert snapshot.warning == "combination odds were not requested"


def test_today_result_payload_is_parsed_and_not_replayable(monkeypatch):
    monkeypatch.setattr(
        "backend.scrapers.final_odds.tokyo_now",
        lambda: datetime(2026, 10, 23, 12, 0, tzinfo=TOKYO),
    )
    body = _api_body("4", {"0103": ["12.4", "0.0", "4"]}, official="2026-10-23 15:40:00")
    page = FetchedBody(
        body, "netkeiba_js", FETCHED, "https://race.netkeiba.com/api/api_get_jra_odds.html"
    )
    quotes = quotes_from_result_api(page, RACE_ID, "umaren")

    assert odds_api_is_final_payload(body) is True
    assert odds_api_is_replayable(body) is False
    assert quotes[0].odds == 12.4
    assert quotes[0].source == "netkeiba_js"
    assert odds_api_is_final_payload('{"status":"result","data":""}') is False
    assert (
        odds_api_is_replayable('{"status":"middle","data":{"odds":{"4":{"0103":["1.2","0","1"]}}}}')
        is False
    )


def test_odds_api_cache_keeps_only_a_past_nonempty_result(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "backend.scrapers.final_odds.tokyo_now",
        lambda: datetime(2026, 10, 23, 12, 0, tzinfo=TOKYO),
    )
    calls: list[str] = []
    bodies = {
        "empty": '{"status":"result","data":""}',
        "past": _api_body("5", {"0102": ["3.8", "4.3", "5"]}, official="2026-09-21 15:53:44"),
        "today": _api_body("5", {"0102": ["3.8", "4.3", "5"]}, official="2026-10-23 15:53:44"),
    }

    class _Transport:
        def get(self, url, params=None, headers=None, encoding=None):
            kind = str((params or {}).get("type"))
            calls.append(kind)
            return bodies[kind]

    url = "https://race.netkeiba.com/api/api_get_jra_odds.html"
    client = CachingHTTPClient(tmp_path, transport=_Transport(), min_request_interval=0)
    client.get(url, params={"type": "empty"})
    client.get(url, params={"type": "empty"})
    client.get(url, params={"type": "past"})
    client.get(url, params={"type": "past"})
    client.get(url, params={"type": "today"})
    client.get(url, params={"type": "today"})
    assert calls == ["empty", "empty", "past", "today", "today"]

    client.replay_disabled = True
    client.get(url, params={"type": "past"})
    client.get(url, params={"type": "past"})
    assert calls == ["empty", "empty", "past", "today", "today", "past", "past"]


def test_today_race_does_not_cache_odds_pages(tmp_path: Path, monkeypatch):
    stamped = datetime(2025, 2, 2, 16, 0, tzinfo=TOKYO)
    monkeypatch.setattr("backend.services.final_odds_service.tokyo_now", lambda: stamped)
    calls: list[str] = []

    class _Transport:
        def get(self, url, params=None, headers=None, encoding=None):
            calls.append(url)
            if "result.html" in url:
                return _result_html()
            return "<html><body>読み込み中</body></html>"

        def post(self, url, data=None, headers=None, encoding=None):
            raise requests.RequestException("jra payout unreachable")

    client = CachingHTTPClient(tmp_path, transport=_Transport(), min_request_interval=0)
    service, _http, _jra = _service(
        _result_html(),
        PublicOddsPages([], None, False),
        http=client,
    )
    first = service.get(RACE_ID, bet_types="wide")
    second = service.get(RACE_ID, bet_types="wide")

    assert first.quotes == []
    assert second.quotes == []
    assert first.fetched_at == stamped
    result_hits = [url for url in calls if "result.html" in url]
    odds_hits = [url for url in calls if "odds/index.html" in url]
    assert result_hits == ["https://race.netkeiba.com/race/result.html"]
    assert odds_hits == [
        "https://race.netkeiba.com/odds/index.html",
        "https://race.netkeiba.com/odds/index.html",
    ]


def test_cli_final_odds_json(monkeypatch, capsys):
    snapshot = FinalRaceOdds(
        race_id=RACE_ID,
        bet_types=["wide"],
        prefer="live",
        status="live",
        reason=None,
        encoding="utf-8",
        race_phase="post",
        result_phase="confirmed",
        complete=True,
        odds_final=True,
        quotes=[
            OddsQuote(
                race_id=RACE_ID,
                bet_type="wide",
                combination="1-3",
                odds_min=2.2,
                odds_max=3.5,
                popularity=2,
                source="jra_html",
                kind="live",
                fetched_at=FETCHED,
            )
        ],
        fetched_at=FETCHED,
    )

    class _Service:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get_final_combination_odds(self, race_id, bet_types=None):
            assert race_id == RACE_ID
            assert bet_types == "wide"
            return snapshot

    monkeypatch.setattr("backend.cli.RaceService", lambda: _Service())
    monkeypatch.setattr(
        sys, "argv", ["backend.cli", "final-odds", RACE_ID, "--bet-types", "wide", "--json"]
    )
    cli_main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["odds_final"] is True
    assert payload["race_phase"] == "post"
    assert payload["result_phase"] == "confirmed"
    assert payload["quotes"][0]["popularity"] == 2
    assert payload["quotes"][0]["odds_min"] == 2.2
    assert payload["fetched_at"].endswith("+09:00")


def test_placeholder_shell_uses_the_result_api():
    """A real odds page prints ---.- and names the API. Prices come from status result."""
    shell = _load("netkeiba_odds_index_202605040711.html")
    assert "---.-" in shell
    assert "apiUrl:'/api/api_get_jra_odds.html'" in shell
    assert "raceId:'202605040711'" in shell
    assert "oddsType:'4'" in shell
    assert final_odds_need_fallback(shell) is True
    assert page_says_final_odds(shell) is False
    assert parse_confirmed_odds([_page(shell, "netkeiba_html")], "202605040711").quotes == []

    race_id = "202605040711"
    api = {
        "4": _load("netkeiba_jra_odds_result_umaren_202605040711.json"),
        "5": _load("netkeiba_jra_odds_result_wide_202605040711.json"),
        "7": _load("netkeiba_jra_odds_result_sanrenpuku_202605040711.json"),
    }
    payouts = "".join(
        [
            _payout_row("ワイド", [("9", "11")], [620], [4]),
            _payout_row("馬連", [("9", "11")], [1770], [5]),
            _payout_row("3連複", [("6", "9", "11")], [3240], [4]),
        ]
    )
    headless = _Headless(_load("jra_last_odds.html"))
    service, client, _jra = _service(
        _result_html(payouts=payouts),
        PublicOddsPages([], None, False),
        odds_html=shell,
        api=api,
        headless_render=headless,
    )
    snapshot = service.get(race_id)

    assert headless.calls == []
    assert snapshot.odds_final is True
    assert snapshot.race_phase == "post"
    assert snapshot.result_phase == "confirmed"
    assert snapshot.complete is True
    assert snapshot.warning is None
    by_type = {
        bet: [quote for quote in snapshot.quotes if quote.bet_type == bet]
        for bet in ("umaren", "wide", "sanrenpuku")
    }
    assert len(by_type["umaren"]) == 78
    assert len(by_type["wide"]) == 78
    assert len(by_type["sanrenpuku"]) == 286
    by_key = _by_key(snapshot.quotes)
    umaren = by_key[("umaren", "9-11")]
    wide = by_key[("wide", "9-11")]
    trio = by_key[("sanrenpuku", "6-9-11")]
    assert umaren.odds == 17.7
    assert umaren.popularity == 5
    assert wide.odds_min == 6.2
    assert wide.odds_max == 6.7
    assert wide.popularity == 4
    assert trio.odds == 32.4
    assert trio.popularity == 4
    assert snapshot.status == "result"
    assert all(quote.source == "netkeiba_result" for quote in snapshot.quotes)
    assert all(quote.kind == "result" for quote in snapshot.quotes)
    assert all(
        quote.market_updated_at is not None
        and quote.market_updated_at.isoformat().endswith("+09:00")
        for quote in snapshot.quotes
    )
    assert snapshot.quotes[0].market_updated_at == datetime(2026, 10, 18, 15, 28, 7, tzinfo=TOKYO)
    assert {str((params or {}).get("type")) for params in client.params} >= {"4", "5", "7"}
    assert any((params or {}).get("action") == "init" for params in client.params)
    assert any((params or {}).get("compress") == "0" for params in client.params)
    _assert_final_clock(snapshot)


def test_missing_result_keeps_the_not_found_warning():
    service, _client, jra = _service(
        "<html><body>missing</body></html>",
        PublicOddsPages([], None, False),
    )
    snapshot = service.get(RACE_ID)

    assert jra.calls == []
    assert snapshot.quotes == []
    assert snapshot.odds_final is False
    assert snapshot.warning == "race result not found"


def test_placeholder_odds_index_is_not_cached(tmp_path: Path):
    calls: list[str] = []

    class _Transport:
        def get(self, url, params=None, headers=None, encoding=None):
            calls.append(url)
            if "priced" in url:
                return "<html><td class='odds'>12.4</td></html>"
            return "<html><td class='Odds'><span>---.-</span></td></html>"

    client = CachingHTTPClient(tmp_path, transport=_Transport(), min_request_interval=0)
    shell = "https://race.netkeiba.com/odds/index.html?race=shell"
    priced = "https://race.netkeiba.com/odds/index.html?race=priced"
    client.get(shell)
    client.get(shell)
    client.get(priced)
    client.get(priced)
    assert calls.count(shell) == 2
    assert calls.count(priced) == 1

    planted = "<html><td class='odds'>---.-</td></html>"
    key = CachingHTTPClient._cache_key("GET", shell, None, None)
    path = client._path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"url": shell, "replayable": True, "body": planted}, ensure_ascii=False),
        encoding="utf-8",
    )
    client.get(shell)
    assert calls.count(shell) == 3


def test_failed_playwright_start_still_sees_the_open_browser(monkeypatch):
    import sys
    import types

    from backend.scrapers import jra_headless

    session = type("Session", (), {"_browser": object()})()
    seen = {"n": 0}

    def _running():
        seen["n"] += 1
        if seen["n"] == 1:
            return None
        return session

    class _Driver:
        def __enter__(self):
            raise RuntimeError("already started")

        def __exit__(self, exc_type, exc, tb):
            return False

    fake_sync = types.ModuleType("playwright.sync_api")
    fake_sync.sync_playwright = lambda: _Driver()  # type: ignore[attr-defined]
    fake_playwright = types.ModuleType("playwright")
    fake_playwright.sync_api = fake_sync  # type: ignore[attr-defined]
    monkeypatch.setattr(jra_headless, "running_shared_session", _running)
    monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync)
    assert headless_browser_installed() is True
    assert seen["n"] == 2


def test_open_shared_browser_renders_without_a_second_driver(monkeypatch):
    import sys
    import types

    from backend.scrapers import jra_headless

    class _Session:
        def __init__(self) -> None:
            self._browser = object()
            self.calls: list[str] = []

        def get(self, url, params=None, headers=None, encoding=None):
            self.calls.append(url)
            return "<html><h1>最終オッズ</h1></html>"

    session = _Session()
    monkeypatch.setattr(jra_headless, "_shared", session)
    fake_sync = types.ModuleType("playwright.sync_api")

    def _boom():
        raise AssertionError("second playwright start")

    fake_sync.sync_playwright = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync)
    url = "https://race.netkeiba.com/odds/index.html?type=b4&race_id=202605040711"
    assert headless_browser_installed() is True
    assert render_headless(url) == "<html><h1>最終オッズ</h1></html>"
    assert session.calls == [url]


def test_final_odds_api_returns_the_snapshot(monkeypatch):
    snapshot = FinalRaceOdds(
        race_id=RACE_ID,
        bet_types=["wide"],
        prefer="live",
        status="unavailable",
        reason="result phase is provisional",
        encoding="utf-8",
        race_phase=None,
        result_phase="provisional",
        complete=True,
        odds_final=False,
        quotes=[],
        warning="result phase is provisional",
        fetched_at=FETCHED,
    )

    def _fake(self, race_id, bet_types=None):
        assert race_id == RACE_ID
        assert bet_types == "wide"
        return snapshot

    monkeypatch.setattr(RaceService, "get_final_combination_odds", _fake)
    with TestClient(app) as client:
        response = client.get(f"/api/races/{RACE_ID}/final-odds?bet_types=wide")
    assert response.status_code == 200
    body = response.json()
    assert body["odds_final"] is False
    assert body["result_phase"] == "provisional"
    assert body["quotes"] == []
    assert body["encoding"] == "utf-8"
    assert body["fetched_at"].endswith("+09:00")
