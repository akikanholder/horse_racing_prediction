"""Historical flat races: final odds, payouts, and the full finish order."""

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

from backend.cli import main as cli_main
from backend.main import app
from backend.models.odds import RaceOdds
from backend.models.race import Race, SettledRaceResult
from backend.scrapers.final_odds import (
    FINAL_ODDS_NOT_CONFIRMED,
    PublicOddsPages,
    parse_confirmed_odds,
    result_is_post_race,
)
from backend.scrapers.jra_scraper import JRAScraper, _ParsedRaceCname
from backend.scrapers.netkeiba_odds_scraper import parse_netkeiba_place_odds
from backend.scrapers.netkeiba_scraper import NetkeibaScraper
from backend.scrapers.page_cache import CachingHTTPClient
from backend.scrapers.page_fallback import (
    AccessBlocked,
    FetchedBody,
    headless_browser_installed,
    render_headless,
)
from backend.services.historical_service import HistoricalService
from backend.services.race_service import RaceService

FIXTURES = Path(__file__).parent / "fixtures"
FETCHED = datetime(2026, 10, 12, 9, 0, 0)
RACE_ID = "202505010101"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _page(body: str, source: str = "jra_html") -> FetchedBody:
    return FetchedBody(
        body=body, source=source, fetched_at=FETCHED, url="https://www.jra.go.jp/odds"
    )


class _QuietHTTP:
    def __init__(self, page: str):
        self.page = page
        self.gets: list[str] = []

    def get(self, url, params=None, headers=None, encoding=None):
        self.gets.append(url)
        return self.page

    def post(self, url, data=None, headers=None, encoding=None):
        raise requests.RequestException("jra payout unreachable")

    def close(self) -> None:
        return None


class _Odds:
    def __init__(self, phase: str = "pre", price: float = 99.9):
        self.phase = phase
        self.price = price
        self.calls: list[str] = []

    def _snapshot(self, bet_type: str) -> RaceOdds:
        self.calls.append(bet_type)
        quotes = []
        if bet_type == "win" and self.phase == "pre":
            from backend.models.odds import OddsQuote

            quotes = [
                OddsQuote(
                    race_id=RACE_ID,
                    bet_type="win",
                    combination="1",
                    odds=self.price,
                    source="netkeiba_live",
                    kind="live",
                    fetched_at=FETCHED,
                )
            ]
        return RaceOdds(
            race_id=RACE_ID,
            bet_types=[bet_type],  # type: ignore[list-item]
            prefer="live",
            status="live" if quotes else "unavailable",
            reason=None if quotes else "result odds empty",
            race_phase=self.phase,  # type: ignore[arg-type]
            quotes=quotes,
            fetched_at=FETCHED,
        )

    def get_win_odds(self, race_id, fetched_at=None):
        return self._snapshot("win")

    def get_place_odds(self, race_id, fetched_at=None):
        return self._snapshot("place")

    def get_wide_odds(self, race_id, fetched_at=None):
        return self._snapshot("wide")

    def get_umaren_odds(self, race_id, fetched_at=None):
        return self._snapshot("umaren")

    def get_sanrenpuku_odds(self, race_id, fetched_at=None):
        return self._snapshot("sanrenpuku")

    def close(self) -> None:
        return None


class _JraPages:
    def __init__(self, html: str | None):
        self.html = html
        self.calls: list[str] = []

    def fetch_odds_pages(self, race_id, fetched_at=None):
        self.calls.append(race_id)
        if self.html is None:
            return PublicOddsPages([], "JRA odds page was not on the card", False)
        return PublicOddsPages([_page(self.html)], None, False)

    def close(self) -> None:
        return None


def _service(html: str, jra_html: str | None, odds: _Odds | None = None) -> HistoricalService:
    client = _QuietHTTP(html)
    return HistoricalService(
        netkeiba=NetkeibaScraper(http_client=client, headless_render="off"),  # type: ignore[arg-type]
        jra=_JraPages(jra_html),  # type: ignore[arg-type]
        odds=odds or _Odds(),
        delay_seconds=0,
    )


def _result_html(
    rows: str, *, surface: str, going: str, race_class: str, venue: str, heads: int
) -> str:
    return f"""<!DOCTYPE html><html><head><title>テスト | 2025年2月2日 {venue}1R</title></head><body>
<div class="RaceList_Date"><dl id="RaceList_DateList"><dd class="Active">
<a href="../top/race_list.html?kaisai_date=20250202">2月2日</a></dd></dl></div>
<div class="RaceList_Item02">
<h1 class="RaceName">平地戦</h1>
<p class="RaceResultState">確定</p>
<div class="RaceData01"><span>{surface}</span> / 天候:晴 <span class="Item03">/ 馬場:{going}</span></div>
<div class="RaceData02"><span>1回</span><span>{venue}</span><span>2日目</span>
<span>{race_class}</span><span>{heads}頭</span></div>
</div>
<table summary="全着順"><tr>
<th>着順</th><th>枠</th><th>馬番</th><th>馬名</th><th>斤量</th><th>騎手</th>
<th>タイム</th><th>着差</th><th>人気</th><th>単勝オッズ</th><th>後3F</th><th>コーナー通過順</th>
</tr>
{rows}
</table>
<table class="Payout_Detail_Table">
<tr><th>単勝</th><td class="Result"><div><span>1</span></div><div><span>4</span></div></td>
<td class="Payout"><span>150円</span><br/><span>160円</span></td>
<td class="Ninki"><span>2人気</span><span>1人気</span></td></tr>
<tr><th>ワイド</th><td class="Result"><ul><li>1</li><li>4</li></ul></td>
<td class="Payout"><span>540円</span></td><td class="Ninki"><span>3人気</span></td></tr>
<tr><th>馬連</th><td class="Result"><ul><li>1</li><li>4</li></ul></td>
<td class="Payout"><span>980円</span></td><td class="Ninki"><span>4人気</span></td></tr>
<tr><th>3連複</th><td class="Result"><ul><li>1</li><li>4</li><li>7</li></ul></td>
<td class="Payout"><span>2400円</span></td><td class="Ninki"><span>5人気</span></td></tr>
</table>
<tr><th>返還</th><td class="Result"><span>2</span></td><td>単勝</td></tr>
</body></html>"""


def _runner(rank: str, number: int, name: str, odds: str, popularity: str) -> str:
    return (
        "<tr class='HorseList'>"
        f"<td class='Result_Num'><div class='Rank'>{rank}</div></td>"
        f"<td><div>1</div></td><td><div>{number}</div></td>"
        f"<td class='Horse_Info'><a href='https://db.netkeiba.com/horse/{number:010d}'>{name}</a></td>"
        "<td><span class='JockeyWeight'>55.0</span></td><td>騎手</td><td></td><td></td>"
        f"<td>{popularity}</td><td>{odds}</td><td></td><td></td></tr>"
    )


def test_jra_final_odds_page_keeps_win_place_and_combos():
    parsed = parse_confirmed_odds([_page(_load("jra_final_odds.html"))], RACE_ID)
    assert parsed.confirmed is True
    by_key = {(quote.bet_type, quote.combination): quote for quote in parsed.quotes}
    assert by_key[("win", "1")].odds == 3.4
    assert by_key[("win", "1")].source == "jra_html"
    assert by_key[("win", "1")].fetched_at == FETCHED
    assert by_key[("win", "1")].withheld is False
    scratched = by_key[("win", "2")]
    assert scratched.odds is None
    assert scratched.withheld is True
    assert scratched.non_runner is True
    assert by_key[("place", "1")].odds is None
    assert by_key[("place", "1")].odds_min == 1.3
    assert by_key[("place", "1")].odds_max == 1.6
    assert by_key[("place", "3")].odds == 2.1
    assert by_key[("place", "2")].withheld is True
    assert by_key[("umaren", "1-3")].odds == 12.4
    assert by_key[("umaren", "1-2")].odds is None
    assert by_key[("umaren", "1-2")].withheld is True
    assert by_key[("wide", "1-3")].odds_min == 2.2
    assert by_key[("wide", "1-3")].odds_max == 3.5
    assert by_key[("sanrenpuku", "1-2-5")].odds == 40.6
    assert {runner.horse_number for runner in parsed.non_runners} == {2}


def test_live_odds_page_is_not_final():
    parsed = parse_confirmed_odds([_page(_load("jra_live_odds.html"))], RACE_ID)
    assert parsed.confirmed is False
    assert parsed.quotes == []


def test_same_site_json_fills_a_final_shell():
    shell = _page(_load("jra_final_odds_shell.html"))
    payload = _page(_load("jra_final_odds.json"), source="jra_js")
    parsed = parse_confirmed_odds([shell, payload], RACE_ID)
    assert parsed.confirmed is True
    win = next(
        quote for quote in parsed.quotes if quote.bet_type == "win" and quote.combination == "1"
    )
    assert win.odds == 4.2
    assert win.source == "jra_js"


def test_place_range_from_post_payload_uses_the_odds_parser():
    payload = {
        "status": "result",
        "data": {"odds": {"2": {"01": ["1.2", "1.5", "1"], "02": ["----", "----", ""]}}},
        "reason": "",
    }
    snapshot = parse_netkeiba_place_odds(payload, RACE_ID, fetched_at=FETCHED)
    assert snapshot.race_phase == "post"
    by_combo = {quote.combination: quote for quote in snapshot.quotes}
    assert by_combo["1"].odds_min == 1.2
    assert by_combo["1"].odds_max == 1.5
    assert by_combo["2"].odds is None
    assert by_combo["2"].withheld is True


def test_dead_heat_keeps_order_and_payouts_and_uses_result_win_odds():
    html = _result_html(
        "".join(
            [
                _runner("1", 1, "アルファ", "2.4", "1"),
                _runner("1同着", 4, "ベータ", "3.1", "2"),
                _runner("3", 7, "ガンマ", "8.0", "3"),
                _runner("取消", 2, "スクラッチ", "", ""),
            ]
        ),
        surface="ダート1800m",
        going="稍重",
        race_class="1勝クラス",
        venue="阪神",
        heads=4,
    )
    odds = _Odds(phase="pre", price=99.9)
    service = _service(html, _load("jra_live_odds.html"), odds)
    batch = service.get(race_id="202509020201")
    race = batch.races[0]
    assert race.in_scope is True
    assert race.surface == "ダート"
    assert race.distance == 1800
    assert race.going == "稍重"
    assert race.race_class == "1勝クラス"
    assert race.field_size == 4
    assert race.flat is True
    assert race.result.result_phase == "confirmed"
    assert race.result.complete is True
    assert [row.rank for row in race.result.results] == [1, 1, 3, None]
    assert race.result.results[-1].status == "scratched"
    assert race.result.results[-1].status_label == "取消"
    assert race.result.refunds is not None
    assert race.result.refunds[0].horse_numbers == [2]
    payouts = {(row.bet_type, row.combination): row.payout for row in race.result.payouts}
    assert payouts[("単勝", "1")] == 150
    assert payouts[("単勝", "4")] == 160
    assert payouts[("ワイド", "1-4")] == 540
    assert payouts[("馬連", "1-4")] == 980
    assert payouts[("3連複", "1-4-7")] == 2400
    assert race.odds_final is True
    assert result_is_post_race(race.result) is True
    win = {quote.combination: quote for quote in race.final_odds or [] if quote.bet_type == "win"}
    assert win["1"].odds == 2.4
    assert win["4"].odds == 3.1
    assert win["2"].odds is None
    assert win["2"].withheld is True
    assert win["2"].non_runner is True
    assert all(quote.odds != 99.9 for quote in race.final_odds or [])
    assert "place" not in odds.calls
    assert race.warning is not None
    assert "place odds were not on the page" in race.warning
    assert FINAL_ODDS_NOT_CONFIRMED not in race.warning


def test_scratch_on_the_result_fixture_does_not_take_live_prices():
    html = _load("netkeiba_race_result.html")
    html = html.replace("20241222", "20250105").replace("2024年12月22日", "2025年1月5日")
    html += (
        "<table><tr><th>返還</th><td class='Result'><span>2</span></td><td>単勝</td></tr></table>"
    )
    service = _service(html, _load("jra_live_odds.html"), _Odds(phase="pre", price=99.9))
    race = service.get(race_id="202406050811").races[0]
    assert race.result.race_date == date(2025, 1, 5)
    assert race.result.venue == "中山"
    assert race.surface == "芝"
    assert race.race_class == "オープン"
    assert race.field_size == 16
    assert race.going == "良"
    assert race.in_scope is True
    scratched = next(row for row in race.result.results if row.horse_number == 2)
    assert scratched.rank is None
    assert scratched.status == "scratched"
    assert scratched.status_label == "取消"
    assert race.result.refunds is not None
    assert race.result.refunds[0].horse_numbers == [2]
    win = {quote.combination: quote for quote in race.final_odds or [] if quote.bet_type == "win"}
    assert win["8"].odds == 10.9
    assert win["2"].withheld is True
    assert win["2"].non_runner is True
    assert all(quote.odds != 99.9 for quote in race.final_odds or [])
    assert race.final_odds
    assert all(quote.source != "netkeiba_live" for quote in race.final_odds)


def test_jra_confirmed_prices_win_over_the_result_table_and_live_odds():
    html = _result_html(
        _runner("1", 1, "アルファ", "9.9", "1"),
        surface="芝1600m",
        going="良",
        race_class="オープン",
        venue="東京",
        heads=1,
    )
    odds = _Odds(phase="pre", price=50.0)
    service = _service(html, _load("jra_final_odds.html"), odds)
    race = service.get(race_id=RACE_ID).races[0]
    win = {quote.combination: quote for quote in race.final_odds or [] if quote.bet_type == "win"}
    assert win["1"].odds == 3.4
    assert win["1"].source == "jra_html"
    assert odds.calls == []
    assert race.odds_final is True
    place = next(quote for quote in race.final_odds or [] if quote.bet_type == "place")
    assert place.odds_min == 1.3


def test_provisional_result_does_not_promote_live_odds():
    html = _result_html(
        _runner("1", 1, "アルファ", "2.0", "1"),
        surface="芝1600m",
        going="良",
        race_class="未勝利",
        venue="京都",
        heads=1,
    ).replace("確定", "確定前")
    service = _service(html, _load("jra_live_odds.html"), _Odds(phase="pre", price=77.0))
    race = service.get(race_id="202508010101").races[0]
    assert race.result.result_phase == "provisional"
    assert race.final_odds is None
    assert race.odds_final is False
    assert race.warning is not None
    assert FINAL_ODDS_NOT_CONFIRMED in race.warning


def test_dates_before_2025_are_not_fetched():
    calls: list[date] = []

    class _List:
        def get_races_by_date(self, target):
            calls.append(target)
            return []

        def get_settled_race_result(self, race_id, fetched_at=None):
            raise AssertionError("should not open a race")

        def close(self):
            return None

    service = HistoricalService(
        netkeiba=_List(),  # type: ignore[arg-type]
        jra=_JraPages(None),  # type: ignore[arg-type]
        odds=_Odds(),
        delay_seconds=0,
    )
    batch = service.get(on_date=date(2024, 12, 22))
    assert calls == []
    assert batch.races == []
    assert batch.warning == "historical races start in 2025"


def test_date_range_is_sequential_and_filters_venue_and_jumps():
    seen: list[str] = []
    sleeps: list[float] = []

    def _race(race_id: str, venue: str, surface: str) -> Race:
        return Race(
            race_id=race_id,
            race_name="テスト",
            race_number=int(race_id[-2:]),
            race_date=date(2025, 3, 1),
            venue=venue,
            distance=1600,
            track_type=surface,
        )

    class _List:
        def get_races_by_date(self, target):
            assert target == date(2025, 3, 1)
            return [
                _race("202505010101", "東京", "芝"),
                _race("202505010102", "東京", "障害"),
                _race("202509010101", "阪神", "ダート"),
                _race("202501010101", "札幌", "芝"),
            ]

        def get_settled_race_result(self, race_id, fetched_at=None):
            seen.append(race_id)
            html = _result_html(
                _runner("1", 1, "アルファ", "2.0", "1"),
                surface="芝1600m" if race_id.startswith("202505") else "ダート1400m",
                going="良",
                race_class="未勝利",
                venue="東京" if race_id.startswith("202505") else "阪神",
                heads=1,
            )
            scraper = NetkeibaScraper(http_client=_QuietHTTP(html), headless_render="off")  # type: ignore[arg-type]
            return scraper.get_settled_race_result(race_id, fetched_at=fetched_at)

        def close(self):
            return None

    service = HistoricalService(
        netkeiba=_List(),  # type: ignore[arg-type]
        jra=_JraPages(None),  # type: ignore[arg-type]
        odds=_Odds(phase="pre"),
        delay_seconds=1.5,
        sleeper=sleeps.append,
    )
    batch = service.get(
        on_date=date(2025, 3, 1),
        venues=["東京", "阪神"],
        flat_only=True,
    )
    assert seen == ["202505010101", "202509010101"]
    assert sleeps == [1.5]
    assert [race.result.venue for race in batch.races] == ["東京", "阪神"]
    assert batch.skipped == 2
    assert {race.surface for race in batch.races} == {"芝", "ダート"}


def test_priority_venues_keep_the_four_meets():
    class _List:
        def get_races_by_date(self, target):
            return [
                Race(
                    race_id="202501010101",
                    race_name="札幌",
                    race_number=1,
                    race_date=target,
                    venue="札幌",
                    distance=1200,
                    track_type="芝",
                )
            ]

        def get_settled_race_result(self, race_id, fetched_at=None):
            raise AssertionError(race_id)

        def close(self):
            return None

    service = HistoricalService(
        netkeiba=_List(),  # type: ignore[arg-type]
        jra=_JraPages(None),  # type: ignore[arg-type]
        odds=_Odds(),
        delay_seconds=0,
    )
    batch = service.get(on_date=date(2025, 6, 1), priority_venues=True)
    assert batch.races == []
    assert batch.venues == ["東京", "京都", "中山", "阪神"]
    assert batch.skipped == 1


def test_export_csv_and_jsonl_split_by_surface(tmp_path: Path):
    html = _result_html(
        _runner("1", 1, "アルファ", "2.4", "1"),
        surface="芝1600m",
        going="良",
        race_class="オープン",
        venue="東京",
        heads=1,
    )
    service = _service(html, None, _Odds(phase="pre"))
    batch = service.get(race_id=RACE_ID)
    assert batch.races[0].in_scope is True
    csv_export = service.export(batch, tmp_path / "csv", "csv")
    assert csv_export.row_count == 1
    assert "芝" in csv_export.surfaces
    runners = (tmp_path / "csv" / "runners.csv").read_text(encoding="utf-8")
    assert "surface" in runners.splitlines()[0]
    assert "芝" in runners
    assert "2.4" in runners
    assert "final_win_odds" in runners
    turf = (tmp_path / "csv" / "runners_芝.csv").read_text(encoding="utf-8")
    assert "アルファ" in turf
    jsonl_export = service.export(batch, tmp_path / "jsonl", "jsonl")
    line = json.loads((tmp_path / "jsonl" / "historical.jsonl").read_text(encoding="utf-8"))
    assert line["surface"] == "芝"
    assert line["result"]["results"][0]["rank"] == 1
    assert jsonl_export.race_count == 1


def test_page_cache_skips_a_second_fetch_and_keeps_fetched_at(tmp_path: Path):
    calls: list[str] = []

    class _Transport:
        def get(self, url, params=None, headers=None, encoding=None):
            calls.append(url)
            return "<html><body>確定オッズ 3.4</body></html>"

        def post(self, url, data=None, headers=None, encoding=None):
            calls.append(url)
            return "<html><body>確定オッズ</body></html>"

    client = CachingHTTPClient(tmp_path, transport=_Transport(), min_request_interval=0)
    first = client.get("https://race.netkeiba.com/race/result.html", params={"race_id": RACE_ID})
    second = client.get("https://race.netkeiba.com/race/result.html", params={"race_id": RACE_ID})
    assert first == second
    assert calls == ["https://race.netkeiba.com/race/result.html"]
    stamped = client.cached_fetched_at(
        "GET", "https://race.netkeiba.com/race/result.html", {"race_id": RACE_ID}
    )
    assert stamped is not None


def test_login_wall_is_not_cached(tmp_path: Path):
    calls: list[int] = []

    class _Transport:
        def get(self, url, params=None, headers=None, encoding=None):
            calls.append(1)
            return "<html><head><title>ログイン</title></head><body><form><input type='password'></form></body></html>"

    client = CachingHTTPClient(tmp_path, transport=_Transport(), min_request_interval=0)
    client.get("https://example.test/login")
    client.get("https://example.test/login")
    assert calls == [1, 1]


def test_jra_odds_chain_reads_same_site_json_and_stops_on_403():
    shutuba = (
        "<html><body><a onclick=\"doAction('/JRADB/accessO.html','pw01ode0105202501010120250105/AB');\">"
        "オッズ</a></body></html>"
    )
    shell = _load("jra_final_odds_shell.html")
    payload = _load("jra_final_odds.json")

    class _Posts:
        def __init__(self, odds_error: Exception | None = None):
            self.gets: list[str] = []
            self.odds_error = odds_error

        def post(self, url, data=None, headers=None, encoding=None):
            cname = (data or {}).get("cname")
            if cname == "shutuba":
                return shutuba
            if self.odds_error is not None:
                raise self.odds_error
            return shell

        def get(self, url, params=None, headers=None, encoding=None):
            self.gets.append(url)
            return payload

    http = _Posts()
    scraper = JRAScraper(http_client=http, headless_render="off")  # type: ignore[arg-type]
    parsed_cname = _ParsedRaceCname(
        cname="shutuba",
        venue_code="05",
        year=2025,
        kai=1,
        day_number=1,
        race_number=1,
        race_date=date(2025, 1, 5),
    )
    scraper._find_race_cname = lambda lookup: parsed_cname  # type: ignore[method-assign]
    loaded = scraper.fetch_odds_pages(RACE_ID, fetched_at=FETCHED)
    assert any(page.source == "jra_js" for page in loaded.pages)
    assert http.gets
    parsed = parse_confirmed_odds(loaded.pages, RACE_ID)
    assert parsed.confirmed is True
    assert any(quote.odds == 4.2 for quote in parsed.quotes)

    blocked_http = _Posts(odds_error=_http_error(403))
    blocked = JRAScraper(http_client=blocked_http, headless_render="off")  # type: ignore[arg-type]
    blocked._find_race_cname = lambda lookup: parsed_cname  # type: ignore[method-assign]
    refused = blocked.fetch_odds_pages(RACE_ID, fetched_at=FETCHED)
    assert refused.blocked is True
    assert refused.warning == "JRA odds returned 403"
    assert refused.pages == []
    assert blocked_http.gets == []


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    response.url = "https://www.jra.go.jp/JRADB/accessO.html"
    return requests.HTTPError(f"http {status}", response=response)


def test_cli_history_json(monkeypatch, capsys):
    html = _result_html(
        _runner("1", 1, "アルファ", "2.4", "1"),
        surface="芝1600m",
        going="良",
        race_class="オープン",
        venue="東京",
        heads=1,
    )
    expected = _service(html, None).get(race_id=RACE_ID)

    class _Service:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get_historical_races(self, **kwargs):
            assert kwargs["race_id"] == RACE_ID
            return expected

        def export_historical_races(self, batch, directory, fmt):
            raise AssertionError("not requested")

    monkeypatch.setattr("backend.cli.RaceService", lambda: _Service())
    monkeypatch.setattr(sys, "argv", ["backend.cli", "history", RACE_ID, "--json"])
    cli_main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["races"][0]["race_id"] == RACE_ID
    assert payload["races"][0]["final_odds"][0]["odds"] == 2.4
    assert payload["races"][0]["result"]["results"][0]["status"] == "finished"


def test_history_api_returns_the_batch(monkeypatch):
    html = _result_html(
        _runner("1", 1, "アルファ", "2.4", "1"),
        surface="芝1600m",
        going="良",
        race_class="オープン",
        venue="中山",
        heads=1,
    )
    expected = _service(html, None).get(race_id=RACE_ID)

    def _fake(self, **kwargs):
        assert kwargs["race_id"] == RACE_ID
        return expected

    monkeypatch.setattr(RaceService, "get_historical_races", _fake)
    with TestClient(app) as client:
        response = client.get(f"/api/races/{RACE_ID}/history")
    assert response.status_code == 200
    body = response.json()
    assert body["races"][0]["surface"] == "芝"
    assert body["races"][0]["result"]["payouts"][0]["bet_type"] == "単勝"
    assert body["races"][0]["final_odds"][0]["fetched_at"]


def test_history_span_requires_a_date():
    with TestClient(app) as client:
        response = client.get("/api/races/history")
    assert response.status_code == 400


def _listed_race(race_id: str, venue: str = "東京") -> Race:
    return Race(
        race_id=race_id,
        race_name="平地",
        race_number=int(race_id[-2:]),
        race_date=date(2025, 3, 1),
        venue=venue,
        distance=1600,
        track_type="芝",
    )


def _blocked_result(race_id: str, warning: str) -> SettledRaceResult:
    return SettledRaceResult(race_id=race_id, fetched_at=FETCHED, warning=warning)


def test_date_range_stops_on_403_and_does_not_retry_or_open_later_races(tmp_path: Path):
    seen: list[str] = []
    odds = _Odds(phase="pre")
    jra = _JraPages(None)
    ids = ["202505010101", "202505010102", "202505010103"]

    class _List:
        def get_races_by_date(self, target):
            assert target == date(2025, 3, 1)
            return [_listed_race(race_id) for race_id in ids]

        def get_settled_race_result(self, race_id, fetched_at=None):
            seen.append(race_id)
            if race_id == ids[1]:
                return _blocked_result(race_id, "race result returned 403")
            html = _result_html(
                _runner("1", 1, "アルファ", "2.4", "1"),
                surface="芝1600m",
                going="良",
                race_class="未勝利",
                venue="東京",
                heads=1,
            )
            scraper = NetkeibaScraper(http_client=_QuietHTTP(html), headless_render="off")  # type: ignore[arg-type]
            return scraper.get_settled_race_result(race_id, fetched_at=fetched_at)

        def close(self):
            return None

    service = HistoricalService(
        netkeiba=_List(),  # type: ignore[arg-type]
        jra=jra,  # type: ignore[arg-type]
        odds=odds,
        delay_seconds=1.5,
        sleeper=lambda _seconds: None,
    )
    batch = service.get(on_date=date(2025, 3, 1))
    assert seen == ids[:2]
    assert seen.count(ids[1]) == 1
    assert jra.calls == [ids[0]]
    # The completed race checks the post-race odds API once. The blocked race does not.
    assert odds.calls == ["win"]
    assert batch.stopped_at_race_id == ids[1]
    assert batch.completed_race_ids == [ids[0]]
    assert batch.remaining_race_ids == ids[1:]
    assert batch.warning is not None
    assert "stopped at 202505010102: race result returned 403" in batch.warning
    assert [race.race_id for race in batch.races] == [ids[0]]

    exported = service.export(batch, tmp_path, "jsonl")
    progress = json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))
    assert progress["stopped_at_race_id"] == ids[1]
    assert progress["completed_race_ids"] == [ids[0]]
    assert progress["remaining_race_ids"] == ids[1:]
    assert "returned 403" in progress["warning"]
    assert exported.stopped_at_race_id == ids[1]
    assert exported.completed_race_ids == [ids[0]]
    assert exported.remaining_race_ids == ids[1:]
    assert exported.warning == batch.warning
    csv_export = service.export(batch, tmp_path / "csv", "csv")
    csv_progress = json.loads((tmp_path / "csv" / "progress.json").read_text(encoding="utf-8"))
    assert csv_progress["remaining_race_ids"] == ids[1:]
    assert csv_export.stopped_at_race_id == ids[1]


def test_login_wall_stops_the_range_without_a_second_request():
    seen: list[str] = []

    class _List:
        def get_races_by_date(self, target):
            return [_listed_race("202505010101"), _listed_race("202505010102")]

        def get_settled_race_result(self, race_id, fetched_at=None):
            seen.append(race_id)
            if race_id.endswith("02"):
                return _blocked_result(race_id, "race result requires login")
            html = _result_html(
                _runner("1", 1, "アルファ", "2.0", "1"),
                surface="芝1600m",
                going="良",
                race_class="未勝利",
                venue="東京",
                heads=1,
            )
            scraper = NetkeibaScraper(http_client=_QuietHTTP(html), headless_render="off")  # type: ignore[arg-type]
            return scraper.get_settled_race_result(race_id, fetched_at=fetched_at)

        def close(self):
            return None

    service = HistoricalService(
        netkeiba=_List(),  # type: ignore[arg-type]
        jra=_JraPages(None),  # type: ignore[arg-type]
        odds=_Odds(),
        delay_seconds=0,
    )
    batch = service.get(on_date=date(2025, 3, 1))
    assert seen == ["202505010101", "202505010102"]
    assert batch.stopped_at_race_id == "202505010102"
    assert batch.completed_race_ids == ["202505010101"]
    assert batch.remaining_race_ids == ["202505010102"]
    assert batch.warning is not None
    assert "requires login" in batch.warning


def test_race_list_403_does_not_open_later_days_or_results():
    listed: list[date] = []
    seen: list[str] = []

    class _List:
        def get_races_by_date(self, target):
            listed.append(target)
            if target == date(2025, 3, 2):
                raise _http_error(403)
            return [_listed_race("202505010101")]

        def get_settled_race_result(self, race_id, fetched_at=None):
            seen.append(race_id)
            html = _result_html(
                _runner("1", 1, "アルファ", "2.0", "1"),
                surface="芝1600m",
                going="良",
                race_class="未勝利",
                venue="東京",
                heads=1,
            )
            scraper = NetkeibaScraper(http_client=_QuietHTTP(html), headless_render="off")  # type: ignore[arg-type]
            return scraper.get_settled_race_result(race_id, fetched_at=fetched_at)

        def close(self):
            return None

    service = HistoricalService(
        netkeiba=_List(),  # type: ignore[arg-type]
        jra=_JraPages(None),  # type: ignore[arg-type]
        odds=_Odds(phase="pre"),
        delay_seconds=0,
    )
    batch = service.get(date_from=date(2025, 3, 1), date_to=date(2025, 3, 3))
    assert listed == [date(2025, 3, 1), date(2025, 3, 2)]
    assert seen == ["202505010101"]
    assert batch.stopped_at_race_id is None
    assert batch.completed_race_ids == ["202505010101"]
    assert batch.remaining_race_ids == []
    assert batch.warning == "race list returned 403"


def test_jra_odds_403_does_not_stop_the_date_range():
    seen: list[str] = []

    class _List:
        def get_races_by_date(self, target):
            return [_listed_race("202505010101"), _listed_race("202505010102")]

        def get_settled_race_result(self, race_id, fetched_at=None):
            seen.append(race_id)
            html = _result_html(
                _runner("1", 1, "アルファ", "2.0", "1"),
                surface="芝1600m",
                going="良",
                race_class="未勝利",
                venue="東京",
                heads=1,
            )
            scraper = NetkeibaScraper(http_client=_QuietHTTP(html), headless_render="off")  # type: ignore[arg-type]
            return scraper.get_settled_race_result(race_id, fetched_at=fetched_at)

        def close(self):
            return None

    class _BlockedJra:
        def fetch_odds_pages(self, race_id, fetched_at=None):
            return PublicOddsPages([], "JRA odds returned 403", True)

        def close(self):
            return None

    service = HistoricalService(
        netkeiba=_List(),  # type: ignore[arg-type]
        jra=_BlockedJra(),  # type: ignore[arg-type]
        odds=_Odds(phase="pre"),
        delay_seconds=0,
    )
    batch = service.get(on_date=date(2025, 3, 1))
    assert seen == ["202505010101", "202505010102"]
    assert batch.stopped_at_race_id is None
    assert batch.completed_race_ids == ["202505010101", "202505010102"]
    assert batch.remaining_race_ids == []


def test_race_list_login_stops_before_any_result():
    seen: list[str] = []

    class _List:
        def get_races_by_date(self, target):
            raise AccessBlocked(401, "requires login")

        def get_settled_race_result(self, race_id, fetched_at=None):
            seen.append(race_id)
            raise AssertionError(race_id)

        def close(self):
            return None

    service = HistoricalService(
        netkeiba=_List(),  # type: ignore[arg-type]
        jra=_JraPages(None),  # type: ignore[arg-type]
        odds=_Odds(),
        delay_seconds=0,
    )
    batch = service.get(on_date=date(2025, 3, 1))
    assert seen == []
    assert batch.stopped_at_race_id is None
    assert batch.completed_race_ids == []
    assert batch.remaining_race_ids == []
    assert batch.warning == "race list requires login"


def test_race_list_403_is_not_swallowed():
    class _HTTP:
        def get(self, url, params=None, headers=None, encoding=None):
            raise _http_error(403)

        def post(self, url, data=None, headers=None, encoding=None):
            raise AssertionError("post")

    scraper = NetkeibaScraper(http_client=_HTTP(), headless_render="off")  # type: ignore[arg-type]
    with pytest.raises(requests.HTTPError):
        scraper.get_races_by_date(date(2025, 3, 1))


def test_resume_from_skips_earlier_races_that_are_not_cached():
    seen: list[str] = []

    class _List:
        def get_races_by_date(self, target):
            return [
                _listed_race(race_id)
                for race_id in ("202505010101", "202505010102", "202505010103")
            ]

        def get_settled_race_result(self, race_id, fetched_at=None):
            seen.append(race_id)
            html = _result_html(
                _runner("1", 1, "アルファ", "2.0", "1"),
                surface="芝1600m",
                going="良",
                race_class="未勝利",
                venue="東京",
                heads=1,
            )
            scraper = NetkeibaScraper(http_client=_QuietHTTP(html), headless_render="off")  # type: ignore[arg-type]
            return scraper.get_settled_race_result(race_id, fetched_at=fetched_at)

        def close(self):
            return None

    service = HistoricalService(
        netkeiba=_List(),  # type: ignore[arg-type]
        jra=_JraPages(None),  # type: ignore[arg-type]
        odds=_Odds(phase="pre"),
        delay_seconds=0,
    )
    batch = service.get(on_date=date(2025, 3, 1), resume_from="202505010102")
    assert seen == ["202505010102", "202505010103"]
    assert batch.completed_race_ids == ["202505010102", "202505010103"]
    assert batch.remaining_race_ids == []
    assert batch.warning is not None
    assert "resume left uncached races: 202505010101" in batch.warning


def test_rerun_uses_cached_pages_and_resumes_at_the_blocked_race(tmp_path: Path):
    ids = ["202505010101", "202505010102", "202505010103"]
    links = "".join(
        f"<a href='/race/result.html?race_id={race_id}'>東京 芝1600m</a>" for race_id in ids
    )
    listing = f"<div class='RaceList'>{links}</div>"
    result = _result_html(
        _runner("1", 1, "アルファ", "2.4", "1"),
        surface="芝1600m",
        going="良",
        race_class="未勝利",
        venue="東京",
        heads=1,
    )

    class _Transport:
        def __init__(self) -> None:
            self.gets: list[tuple[str, str | None]] = []
            self.blocked: set[str] = {ids[1]}

        def get(self, url, params=None, headers=None, encoding=None):
            race_id = None if params is None else params.get("race_id")
            self.gets.append((url, race_id))
            if "race_list" in url:
                return listing
            if race_id in self.blocked:
                raise _http_error(403)
            return result

        def post(self, url, data=None, headers=None, encoding=None):
            return "<html><body>パラメータエラー</body></html>"

    transport = _Transport()
    http = CachingHTTPClient(tmp_path, transport=transport, min_request_interval=0)

    def _open() -> HistoricalService:
        return HistoricalService(
            netkeiba=NetkeibaScraper(http_client=http, headless_render="off"),  # type: ignore[arg-type]
            jra=_JraPages(None),  # type: ignore[arg-type]
            odds=_Odds(phase="pre"),
            delay_seconds=1.5,
            sleeper=lambda _seconds: None,
        )

    first = _open().get(on_date=date(2025, 3, 1))
    assert first.stopped_at_race_id == ids[1]
    assert first.completed_race_ids == [ids[0]]
    assert first.remaining_race_ids == [ids[1], ids[2]]
    assert [race_id for url, race_id in transport.gets if race_id == ids[0]] == [ids[0]]
    assert [race_id for url, race_id in transport.gets if race_id == ids[1]] == [ids[1]]
    assert ids[2] not in {race_id for _url, race_id in transport.gets}

    marked = len(transport.gets)
    second = _open().get(on_date=date(2025, 3, 1))
    fresh = transport.gets[marked:]
    assert second.stopped_at_race_id == ids[1]
    assert second.completed_race_ids == [ids[0]]
    assert [race_id for _url, race_id in fresh] == [ids[1]]
    assert all("race_list" not in url for url, _race_id in fresh)

    transport.blocked.clear()
    marked = len(transport.gets)
    third = _open().get(on_date=date(2025, 3, 1), resume_from=ids[1])
    fresh = transport.gets[marked:]
    assert [race_id for _url, race_id in fresh if race_id] == [ids[1], ids[2]]
    assert third.stopped_at_race_id is None
    assert third.completed_race_ids == ids
    assert third.remaining_race_ids == []


def test_history_passes_resume_from(monkeypatch):
    captured: dict[str, object] = {}

    class _Service:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get_historical_races(self, **kwargs):
            captured.update(kwargs)
            return _service(
                _result_html(
                    _runner("1", 1, "アルファ", "2.4", "1"),
                    surface="芝1600m",
                    going="良",
                    race_class="オープン",
                    venue="東京",
                    heads=1,
                ),
                None,
            ).get(race_id=RACE_ID)

        def export_historical_races(self, batch, directory, fmt):
            raise AssertionError("not requested")

    monkeypatch.setattr("backend.cli.RaceService", lambda: _Service())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backend.cli",
            "history",
            "--date",
            "2025-03-01",
            "--resume-from",
            "202505010102",
            "--json",
        ],
    )
    cli_main()
    assert captured["resume_from"] == "202505010102"
    assert captured["on_date"] == date(2025, 3, 1)


def test_history_api_forwards_resume_from(monkeypatch):
    captured: dict[str, object] = {}

    def _fake(self, **kwargs):
        captured.update(kwargs)
        html = _result_html(
            _runner("1", 1, "アルファ", "2.4", "1"),
            surface="芝1600m",
            going="良",
            race_class="オープン",
            venue="東京",
            heads=1,
        )
        return _service(html, None).get(race_id=RACE_ID)

    monkeypatch.setattr(RaceService, "get_historical_races", _fake)
    with TestClient(app) as client:
        response = client.get(
            "/api/races/history", params={"date": "2025-03-01", "resume_from": "202505010102"}
        )
    assert response.status_code == 200
    assert captured["resume_from"] == "202505010102"
    body = response.json()
    assert "stopped_at_race_id" in body
    assert "completed_race_ids" in body
    assert "remaining_race_ids" in body


@pytest.mark.skipif(
    not headless_browser_installed(),
    reason="headless browser is not installed; no CI workflow installs Playwright",
)
def test_headless_render_reads_a_final_odds_page(tmp_path: Path):
    page = tmp_path / "odds.html"
    page.write_text(_load("jra_final_odds.html"), encoding="utf-8")
    rendered = render_headless(page.as_uri())
    parsed = parse_confirmed_odds(
        [FetchedBody(rendered, "headless", FETCHED, page.as_uri())],
        RACE_ID,
    )
    assert "確定オッズ" in rendered
    assert parsed.confirmed is True
    assert any(quote.odds == 3.4 and quote.source == "headless" for quote in parsed.quotes)
