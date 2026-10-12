"""Settled race result: one race's finish and payouts, not horse near-runs."""

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
import requests
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from backend.cli import main as cli_main
from backend.main import app
from backend.models.horse import Horse
from backend.models.race import Race, RaceEntry
from backend.scrapers.jra_payout import (
    JRA_PAYOUT_FORBIDDEN,
    JRA_PAYOUT_MISMATCH,
    JRA_PAYOUT_UNREACHABLE,
)
from backend.scrapers.netkeiba_scraper import (
    RACE_RESULT_EMPTY,
    RACE_RESULT_FETCH_FAILED,
    RACE_RESULT_FORBIDDEN,
    RACE_RESULT_HEADLESS_UNAVAILABLE,
    RACE_RESULT_LOGIN,
    RACE_RESULT_NOT_FOUND,
    RACE_RESULT_NOT_SETTLED,
    RACE_RESULT_PAYOUT_BLANK,
    RACE_RESULT_PAYOUT_MISSING,
    RACE_RESULT_PAYOUT_UNPAIRED,
    RACE_RESULT_PHASE_UNKNOWN,
    RACE_RESULT_TABLE_MALFORMED,
    NetkeibaScraper,
)
from backend.scrapers.page_fallback import headless_browser_installed, render_headless
from backend.services.race_service import RaceService

FIXTURES = Path(__file__).parent / "fixtures"
RACE_ID = "202406050811"
FETCHED_AT = datetime(2026, 9, 27, 12, 0, 0)
RESULT_URL = "https://race.netkeiba.com/race/result.html"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class _FakeHTTP:
    def __init__(
        self,
        page: str | Exception,
        routes: dict[str, str | Exception] | None = None,
        posts: dict[str, str | Exception] | None = None,
    ):
        self.page = page
        self.routes = routes or {}
        self.posts = posts
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, encoding=None):
        self.calls.append(
            {"url": url, "params": params, "headers": headers or {}, "encoding": encoding}
        )
        if url in self.routes:
            body = self.routes[url]
            if isinstance(body, Exception):
                raise body
            return body
        if isinstance(self.page, Exception):
            raise self.page
        return self.page

    def post(self, url, data=None, headers=None, encoding=None):
        self.calls.append(
            {
                "url": url,
                "params": data,
                "headers": headers or {},
                "encoding": encoding,
                "method": "post",
            }
        )
        if self.posts is None:
            raise requests.RequestException("jra payout unreachable")
        cname = (data or {}).get("cname", "")
        if cname not in self.posts:
            raise requests.RequestException("jra payout unreachable")
        body = self.posts[cname]
        if isinstance(body, Exception):
            raise body
        return body

    def close(self) -> None:
        return None


def _scraper(
    page: str | Exception,
    *,
    routes: dict[str, str | Exception] | None = None,
    posts: dict[str, str | Exception] | None = None,
    headless: object = "off",
) -> tuple[NetkeibaScraper, _FakeHTTP]:
    client = _FakeHTTP(page, routes=routes, posts=posts)
    return NetkeibaScraper(http_client=client, headless_render=headless), client  # type: ignore[arg-type]


def _settled(
    page: str | Exception,
    race_id: str = RACE_ID,
    **kwargs: object,
):
    scraper, client = _scraper(page, **kwargs)  # type: ignore[arg-type]
    return scraper.get_settled_race_result(race_id, fetched_at=FETCHED_AT), client


def test_arima_kinen_finish_and_payouts_come_from_the_result_page():
    settled, client = _settled(_load("netkeiba_race_result.html"))

    assert settled.warning == f"{RACE_RESULT_PHASE_UNKNOWN}; {JRA_PAYOUT_UNREACHABLE}"
    assert settled.payout_note is None
    assert settled.complete is True
    assert settled.result_phase is None
    assert settled.source == "netkeiba_html"
    assert settled.race_id == RACE_ID
    assert settled.race_name == "有馬記念"
    assert settled.race_date == date(2024, 12, 22)
    assert settled.race_date != date(2024, 6, 5)
    assert settled.venue == "中山"
    assert settled.distance == 2500
    assert settled.track_type == "芝"
    assert settled.track_condition == "良"
    assert settled.weather == "晴"
    assert settled.fetched_at == FETCHED_AT
    assert len(settled.results) == 16
    assert [row.finish_time for row in settled.results if row.finish_time] != ["6.8"]

    winner = settled.results[0]
    assert winner.rank == 1
    assert winner.status == "finished"
    assert winner.status_label is None
    assert winner.demoted is False
    assert winner.source == "netkeiba_html"
    assert winner.fetched_at == FETCHED_AT
    assert winner.bracket_number == 4
    assert winner.horse_number == 8
    assert winner.horse_name == "レガレイラ"
    assert winner.horse_id == "2021105898"
    assert winner.weight == 54.0
    assert winner.jockey_name == "戸崎圭"
    assert winner.finish_time == "2:31.8"
    assert winner.margin is None
    assert winner.last_3f == 34.9
    assert winner.popularity == 4
    assert winner.odds == 10.9
    assert winner.passing_order == "6-5-5-3"

    second = settled.results[1]
    assert second.rank == 2
    assert second.horse_number == 16
    assert second.horse_name == "シャフリヤール"
    assert second.horse_id == "2018105165"
    assert second.margin == "ハナ"
    assert second.odds == 30.1
    assert second.finish_time == "2:31.8"

    third = settled.results[2]
    assert third.horse_number == 1
    assert third.horse_name == "ダノンデサイル"
    assert third.margin == "1.1/2"
    assert third.weight == 56.0

    scratched = settled.results[-1]
    assert scratched.rank is None
    assert scratched.status == "scratched"
    assert scratched.status_label == "取消"
    assert scratched.demoted is False
    assert scratched.horse_number == 2
    assert scratched.horse_name == "ドウデュース"
    assert scratched.horse_id == "2019105283"
    assert scratched.jockey_name == "武豊"
    assert scratched.weight == 58.0
    assert scratched.finish_time is None
    assert scratched.odds is None
    assert scratched.popularity is None
    assert scratched.last_3f is None

    by_type: dict[str, list[tuple[str, int, int | None]]] = {}
    for payout in settled.payouts:
        by_type.setdefault(payout.bet_type, []).append(
            (payout.combination, payout.payout, payout.popularity)
        )
    assert by_type["単勝"] == [("8", 1090, 4)]
    assert by_type["複勝"] == [("8", 320, 5), ("16", 540, 10), ("1", 210, 2)]
    assert by_type["枠連"] == [("4-8", 6980, 25)]
    assert by_type["馬連"] == [("8-16", 20470, 49)]
    assert by_type["ワイド"] == [("8-16", 4450, 47), ("1-8", 720, 6), ("1-16", 1900, 24)]
    assert by_type["馬単"] == [("8-16", 33100, 86)]
    assert by_type["3連複"] == [("1-8-16", 20850, 70)]
    assert by_type["3連単"] == [("8-16-1", 196520, 548)]

    assert client.calls[0]["url"] == RESULT_URL
    assert client.calls[0]["params"] == {"race_id": RACE_ID}
    assert client.calls[0]["encoding"] == "utf-8"
    headers = client.calls[0]["headers"]
    assert "Chrome/140" in headers["User-Agent"]
    assert "text/html" in headers["Accept"]
    assert headers["Referer"] == "https://race.netkeiba.com/top/"


def test_reordered_columns_still_use_header_names():
    soup = BeautifulSoup(_load("netkeiba_race_result.html"), "lxml")
    table = soup.find("table", summary="全着順")
    assert table is not None
    header = table.find("tr")
    first = header.find_next("tr")
    header_cells = list(header.find_all(["th", "td"], recursive=False))
    data_cells = list(first.find_all("td", recursive=False))
    for cell in header_cells + data_cells:
        cell.extract()
    for cell in reversed(header_cells):
        header.append(cell)
    for cell in reversed(data_cells):
        first.append(cell)

    scraper, _client = _scraper("")
    settled = scraper._parse_settled_race_result(soup, RACE_ID, FETCHED_AT)
    winner = settled.results[0]
    assert winner.horse_number == 8
    assert winner.horse_name == "レガレイラ"
    assert winner.odds == 10.9
    assert winner.last_3f == 34.9
    assert winner.rank == 1
    assert winner.finish_time == "2:31.8"


def test_missing_bet_type_is_noted_and_not_priced():
    html = _load("netkeiba_race_result.html").replace(
        '<tr class="Wakuren">', '<tr class="Wakuren" data-drop="1">'
    )
    soup = BeautifulSoup(html, "lxml")
    wakuren = soup.select_one("tr.Wakuren")
    assert wakuren is not None
    wakuren.decompose()
    scraper, _client = _scraper("")
    settled = scraper._parse_settled_race_result(soup, RACE_ID, FETCHED_AT)
    assert all(payout.bet_type != "枠連" for payout in settled.payouts)
    assert all(payout.payout != 6980 for payout in settled.payouts)
    assert settled.payout_note == f"{RACE_RESULT_PAYOUT_MISSING}: 枠連"
    assert settled.results[0].horse_name == "レガレイラ"


def test_blank_payout_price_is_not_invented():
    html = _load("netkeiba_race_result.html").replace("540円", "---")
    settled, _client = _settled(html)
    place = [payout for payout in settled.payouts if payout.bet_type == "複勝"]
    assert [(payout.combination, payout.payout) for payout in place] == [("8", 320), ("1", 210)]
    assert all(payout.payout != 540 for payout in settled.payouts)
    assert settled.payout_note == f"{RACE_RESULT_PAYOUT_BLANK}: 複勝"


def test_unpaired_payout_row_is_dropped():
    html = _load("netkeiba_race_result.html").replace("1,090円", "")
    settled, _client = _settled(html)
    assert all(payout.bet_type != "単勝" for payout in settled.payouts)
    assert all(payout.payout != 1090 for payout in settled.payouts)
    assert settled.payout_note == f"{RACE_RESULT_PAYOUT_UNPAIRED}: 単勝"
    assert any(payout.bet_type == "馬連" and payout.payout == 20470 for payout in settled.payouts)


def test_venue_and_date_are_not_taken_from_the_race_id():
    soup = BeautifulSoup(_load("netkeiba_race_result.html"), "lxml")
    for span in soup.select(".RaceData02 span"):
        if span.get_text(strip=True) == "中山":
            span.decompose()
    date_list = soup.select_one("#RaceList_DateList")
    assert date_list is not None
    date_list.decompose()
    scraper, _client = _scraper("")
    settled = scraper._parse_settled_race_result(soup, RACE_ID, FETCHED_AT)
    assert settled.venue is None
    assert settled.race_date == date(2024, 12, 22)
    assert settled.results[0].horse_number == 8


def test_abbreviated_going_on_the_result_page():
    html = """
    <div class="RaceData01">
      15:40発走 / <span>芝1200m</span> (右 外 C) / 天候:曇
      <span class="Item03">/ 馬場:稍</span>
    </div>
    """
    scraper, _client = _scraper("")
    header = scraper._settled_header(BeautifulSoup(html, "lxml"))
    assert header.distance == 1200
    assert header.track_type == "芝"
    assert header.track_condition == "稍重"
    assert header.weather == "曇"
    assert header.venue is None


def test_unsettled_race_keeps_the_header_and_empty_results():
    settled, _client = _settled(_load("netkeiba_race_result_unsettled.html"), "202605040111")
    assert settled.warning == RACE_RESULT_NOT_SETTLED
    assert settled.race_name == "グリーンチャンネルC"
    assert settled.race_date == date(2026, 10, 3)
    assert settled.venue == "東京"
    assert settled.distance == 1600
    assert settled.track_type == "ダート"
    assert settled.track_condition is None
    assert settled.weather is None
    assert settled.results == []
    assert settled.payouts == []
    assert settled.payout_note is None


def test_malformed_result_table_does_not_invent_rows():
    settled, _client = _settled(_load("netkeiba_race_result_malformed.html"))
    assert settled.warning == RACE_RESULT_TABLE_MALFORMED
    assert settled.race_name == "有馬記念"
    assert settled.results == []
    assert settled.payouts == []


def test_empty_result_table_warns():
    settled, _client = _settled(_load("netkeiba_race_result_empty.html"))
    assert settled.warning == RACE_RESULT_EMPTY
    assert settled.race_name == "有馬記念"
    assert settled.race_date == date(2024, 12, 22)
    assert settled.venue == "中山"
    assert settled.results == []
    assert settled.payouts == []


def test_missing_race_page_warns():
    settled, _client = _settled(_load("netkeiba_race_result_not_found.html"), "999999999999")
    assert settled.warning == RACE_RESULT_NOT_FOUND
    assert settled.race_name is None
    assert settled.race_date is None
    assert settled.venue is None
    assert settled.distance is None
    assert settled.results == []
    assert settled.payouts == []


def test_fetch_failure_returns_empty_result():
    settled, _client = _settled(requests.RequestException("timeout"))
    assert settled.warning == RACE_RESULT_FETCH_FAILED
    assert settled.race_id == RACE_ID
    assert settled.fetched_at == FETCHED_AT
    assert settled.results == []
    assert settled.payouts == []
    assert settled.race_name is None


def test_service_reads_the_settled_result_from_netkeiba():
    expected, _client = _settled(_load("netkeiba_race_result.html"))

    class _Netkeiba:
        def get_settled_race_result(self, race_id: str):
            assert race_id == RACE_ID
            return expected

    service = RaceService()
    service._netkeiba_scraper = _Netkeiba()
    assert service.get_settled_race_result(RACE_ID).race_name == "有馬記念"


def test_result_api_returns_the_settled_payload(monkeypatch):
    settled, _client = _settled(_load("netkeiba_race_result.html"))

    def _fake(self, race_id: str):
        assert race_id == RACE_ID
        return settled

    monkeypatch.setattr(RaceService, "get_settled_race_result", _fake)
    with TestClient(app) as client:
        response = client.get(f"/api/races/{RACE_ID}/result")
    assert response.status_code == 200
    body = response.json()
    assert body["race_name"] == "有馬記念"
    assert body["race_date"] == "2024-12-22"
    assert body["results"][0]["horse_name"] == "レガレイラ"
    assert body["results"][0]["odds"] == 10.9
    assert body["payouts"][0]["bet_type"] == "単勝"
    assert body["payouts"][0]["payout"] == 1090
    assert body["warning"] == f"{RACE_RESULT_PHASE_UNKNOWN}; {JRA_PAYOUT_UNREACHABLE}"
    assert body["complete"] is True
    assert body["result_phase"] is None
    assert body["results"][0]["source"] == "netkeiba_html"
    assert body["results"][0]["fetched_at"].startswith("2026-09-27T12:00:00")
    assert body["results"][-1]["status"] == "scratched"
    assert body["results"][-1]["status_label"] == "取消"
    assert body["results"][-1]["rank"] is None
    assert body["payouts"][0]["source"] == "netkeiba_html"
    assert "performances" not in body


def test_result_api_returns_warning_without_a_fake_finish(monkeypatch):
    settled, _client = _settled(_load("netkeiba_race_result_unsettled.html"), "202605040111")

    monkeypatch.setattr(RaceService, "get_settled_race_result", lambda self, race_id: settled)
    with TestClient(app) as client:
        response = client.get("/api/races/202605040111/result")
    assert response.status_code == 200
    body = response.json()
    assert body["warning"] == RACE_RESULT_NOT_SETTLED
    assert body["results"] == []
    assert body["payouts"] == []


def test_cli_result_json(monkeypatch, capsys):
    settled, _client = _settled(_load("netkeiba_race_result.html"))

    class _Service:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get_settled_race_result(self, race_id):
            assert race_id == RACE_ID
            return settled

    monkeypatch.setattr("backend.cli.RaceService", lambda: _Service())
    monkeypatch.setattr(sys, "argv", ["backend.cli", "result", RACE_ID, "--json"])
    cli_main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["race_id"] == RACE_ID
    assert payload["race_name"] == "有馬記念"
    assert payload["fetched_at"].startswith("2026-09-27T12:00:00")
    assert payload["results"][0]["horse_number"] == 8
    assert payload["results"][-1]["rank"] is None
    assert payload["payouts"][0] == {
        "bet_type": "単勝",
        "combination": "8",
        "payout": 1090,
        "popularity": 4,
        "source": "netkeiba_html",
        "fetched_at": "2026-09-27T12:00:00",
    }
    assert payload["results"][-1]["status"] == "scratched"
    assert payload["results"][-1]["status_label"] == "取消"
    assert payload["complete"] is True


def test_cli_result_text_says_this_is_not_near_run_history(monkeypatch, capsys):
    settled, _client = _settled(_load("netkeiba_race_result_not_found.html"))

    class _Service:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get_settled_race_result(self, race_id):
            return settled

    monkeypatch.setattr("backend.cli.RaceService", lambda: _Service())
    monkeypatch.setattr(sys, "argv", ["backend.cli", "result", RACE_ID])
    cli_main()
    output = capsys.readouterr().out
    assert "近走ではない" in output
    assert "警告: race result not found" in output
    assert "なし" in output


def test_cli_help_separates_result_from_horse_history(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["backend.cli", "result", "--help"])
    with pytest.raises(SystemExit) as caught:
        cli_main()
    assert caught.value.code == 0
    output = capsys.readouterr().out
    assert "近走" in output
    assert "期待値" in output

    monkeypatch.setattr(sys, "argv", ["backend.cli", "horse", "--help"])
    with pytest.raises(SystemExit) as caught:
        cli_main()
    assert caught.value.code == 0
    horse_help = capsys.readouterr().out
    assert "近走" in horse_help
    assert "確定したレース結果ではない" in horse_help


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    error = requests.HTTPError(f"http {status}")
    error.response = response
    return error


def _row(
    rank: str,
    number: int,
    name: str,
    *,
    demoted: bool = False,
    horse_id: str | None = None,
) -> str:
    mark = "<span>降着</span>" if demoted else ""
    href = horse_id or f"{number:010d}"
    return (
        "<tr class='HorseList'>"
        f"<td class='Result_Num'><div class='Rank'>{rank}</div>{mark}</td>"
        f"<td><div>1</div></td><td><div>{number}</div></td>"
        "<td class='Horse_Info'><span class='Horse_Name'>"
        f"<a href='https://db.netkeiba.com/horse/{href}'>{name}</a></span></td>"
        "<td><span class='JockeyWeight'>55.0</span></td><td>騎手</td>"
        "<td></td><td></td><td></td><td></td><td></td><td></td>"
        "</tr>"
    )


def _result_html(
    rows: list[str],
    *,
    heads: int,
    phase: str | None = None,
    extra: str = "",
    title: str = "ステークス | 2024年12月22日 中山11R",
) -> str:
    phase_html = f"<p class='RaceResultState'>{phase}</p>" if phase else ""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title}</title></head><body>
<div class="RaceList_Date"><dl id="RaceList_DateList"><dd class="Active">
<a href="../top/race_list.html?kaisai_date=20241222">12月22日</a></dd></dl></div>
<div class="RaceList_Item02">
<h1 class="RaceName">ステークス</h1>
{phase_html}
<div class="RaceData01"><span>芝1600m</span> / 天候:晴 <span class="Item03">/ 馬場:良</span></div>
<div class="RaceData02"><span>5回</span><span>中山</span><span>8日目</span><span>{heads}頭</span></div>
</div>
<table summary="全着順"><tr>
<th>着順</th><th>枠</th><th>馬番</th><th>馬名</th><th>斤量</th><th>騎手</th>
<th>タイム</th><th>着差</th><th>人気</th><th>単勝オッズ</th><th>後3F</th><th>コーナー通過順</th>
</tr>
{"".join(rows)}
</table>
{extra}
</body></html>"""


_WIN_PAYOUT = """
<table class="Payout_Detail_Table"><tr class="Tansho"><th>単勝</th>
<td class="Result"><div><span>1</span></div></td>
<td class="Payout"><span>100円</span></td>
<td class="Ninki"><span>1人気</span></td></tr></table>
"""

_JRA_CNAME = "pw01hde01062024050820241222/AA"

_JRA_INDEX = (
    "<html><body><a onclick=\"return doAction('/JRADB/accessH.html', "
    f"'{_JRA_CNAME}');\">5回中山8日</a></body></html>"
)


def _jra_day(yen: str, *, umaren: str | None = None) -> str:
    extra = ""
    if umaren is not None:
        extra = f"""<li><dl><dt>馬連</dt><dd>
<div class="line"><div class="num">1-2</div>
<div class="yen">{umaren}<span class="unit">円</span></div>
<div class="pop">2<span>番人気</span></div></div>
</dd></dl></li>"""
    return f"""<html><body>
<img src="/JRADB/img/race_number_main/race_num_11.png" alt="11レース">
<div class="refund_unit"><ul><li class="win"><dl><dt>単勝</dt><dd>
<div class="line"><div class="num">1</div>
<div class="yen">{yen}<span class="unit">円</span></div>
<div class="pop">1<span>番人気</span></div></div>
</dd></dl></li>{extra}</ul></div>
</body></html>"""


def _runners_json(names: list[tuple[str, int, str]]) -> str:
    runners = []
    for rank, number, name in names:
        runners.append(
            {
                "rank": rank,
                "horse_number": number,
                "horse_name": name,
                "horse_id": f"{number:010d}",
            }
        )
    return json.dumps(
        {"entry_count": len(names), "result_phase": "confirmed", "runners": runners},
        ensure_ascii=False,
    )


def test_full_field_keeps_every_status_and_the_official_demotion():
    html = _result_html(
        [
            _row("1", 1, "イチ"),
            _row("2", 2, "ニ", demoted=True),
            _row("取消", 3, "サン"),
            _row("除外", 4, "ヨン"),
            _row("競走中止", 5, "ゴ"),
            _row("失格", 6, "ロク"),
            _row("再審査", 7, "ナナ"),
        ],
        heads=7,
        phase="確定",
    )
    settled, _client = _settled(html)
    by_number = {row.horse_number: row for row in settled.results}
    assert settled.complete is True
    assert settled.result_phase == "confirmed"
    assert settled.warning is None
    assert by_number[1].status == "finished"
    assert by_number[1].rank == 1
    assert by_number[1].status_label is None
    assert by_number[2].status == "finished"
    assert by_number[2].rank == 2
    assert by_number[2].demoted is True
    assert by_number[3].status == "scratched"
    assert by_number[3].status_label == "取消"
    assert by_number[3].rank is None
    assert by_number[4].status == "excluded"
    assert by_number[4].status_label == "除外"
    assert by_number[4].rank is None
    assert by_number[5].status == "did_not_finish"
    assert by_number[5].status_label == "競走中止"
    assert by_number[5].rank is None
    assert by_number[6].status == "disqualified"
    assert by_number[6].status_label == "失格"
    assert by_number[6].rank is None
    assert by_number[7].status == "unknown"
    assert by_number[7].status_label == "再審査"
    assert by_number[7].rank is None
    assert all(
        row.source == "netkeiba_html" and row.fetched_at == FETCHED_AT for row in settled.results
    )


def test_provisional_phase_is_read_from_the_printed_label():
    html = _result_html([_row("1", 1, "イチ"), _row("2", 2, "ニ")], heads=2, phase="結果確定前")
    settled, _client = _settled(html)
    assert settled.result_phase == "provisional"
    assert settled.complete is True
    assert settled.warning is None


def test_truncated_table_uses_embedded_json_and_marks_the_source():
    field = [
        ("1", 1, "イチ"),
        ("2", 2, "ニ"),
        ("3", 3, "サン"),
        ("4", 4, "ヨン"),
        ("5", 5, "ゴ"),
        ("取消", 6, "ロク"),
        ("除外", 7, "ナナ"),
        ("中止", 8, "ハチ"),
    ]
    html = _result_html(
        [_row(rank, number, name) for rank, number, name in field[:5]],
        heads=8,
        extra=(
            '<script type="application/json" id="race-result-data">'
            + _runners_json(field)
            + "</script>"
        ),
    )
    rendered: list[str] = []
    settled, client = _settled(html, headless=lambda url: rendered.append(url) or "")
    assert rendered == []
    assert len(client.calls) == 1
    assert settled.complete is True
    assert settled.result_phase == "confirmed"
    assert settled.warning is None
    assert settled.source == "netkeiba_html+netkeiba_js"
    by_source = {row.horse_number: row.source for row in settled.results}
    assert by_source[1] == "netkeiba_html"
    assert by_source[6] == "netkeiba_js"
    assert by_source[8] == "netkeiba_js"
    scratch = next(row for row in settled.results if row.horse_number == 6)
    assert scratch.status == "scratched"
    assert scratch.status_label == "取消"
    assert scratch.rank is None
    stopped = next(row for row in settled.results if row.horse_number == 8)
    assert stopped.status == "did_not_finish"
    assert stopped.status_label == "中止"
    assert all(row.fetched_at == FETCHED_AT for row in settled.results)


def test_truncated_table_fetches_the_result_json_the_page_calls():
    field = [(str(n), n, f"馬{n}") for n in range(1, 9)]
    endpoint = "https://race.netkeiba.com/api/api_get_race_result.html?race_id=202406050811"
    html = _result_html(
        [_row(rank, number, name) for rank, number, name in field[:5]],
        heads=8,
        extra=f"<script>$.ajax({{url: '{endpoint}'}});</script>",
    )
    settled, client = _settled(html, routes={endpoint: _runners_json(field)})
    assert any(call["url"] == endpoint for call in client.calls)
    assert settled.complete is True
    assert settled.result_phase == "confirmed"
    assert len(settled.results) == 8
    assert settled.results[5].source == "netkeiba_js"


def test_truncated_table_stays_incomplete_when_nothing_else_is_published():
    html = _result_html(
        [_row(str(n), n, f"馬{n}") for n in range(1, 6)],
        heads=8,
    )
    settled, _client = _settled(html)
    assert settled.complete is False
    assert len(settled.results) == 5
    assert settled.warning == "race result incomplete: 5 of 8 runners; " + RACE_RESULT_PHASE_UNKNOWN
    assert all(row.rank == row.horse_number for row in settled.results)


def test_missing_headless_browser_is_a_warning_not_an_error(monkeypatch):
    monkeypatch.setattr(
        "backend.scrapers.page_fallback.headless_browser_installed",
        lambda: False,
    )
    html = _result_html([_row(str(n), n, f"馬{n}") for n in range(1, 6)], heads=8)
    settled, _client = _settled(html, headless="auto")
    assert settled.complete is False
    assert settled.warning is not None
    assert "5 of 8 runners" in settled.warning
    assert RACE_RESULT_HEADLESS_UNAVAILABLE in settled.warning


def test_headless_render_fills_the_runners_the_table_omitted():
    short = _result_html([_row(str(n), n, f"馬{n}") for n in range(1, 6)], heads=8)
    full = _result_html(
        [_row(str(n), n, f"馬{n}") for n in range(1, 9)],
        heads=8,
        phase="確定",
    )
    seen: list[str] = []

    def _render(url: str) -> str:
        seen.append(url)
        return full

    settled, _client = _settled(short, headless=_render)
    assert seen and "race_id=202406050811" in seen[0]
    assert settled.complete is True
    assert settled.result_phase == "confirmed"
    assert settled.warning is None
    assert {row.horse_number for row in settled.results} == set(range(1, 9))
    assert settled.results[0].source == "netkeiba_html"
    assert settled.results[-1].source == "headless"
    assert settled.source == "netkeiba_html+headless"


def test_forbidden_result_does_not_try_json_or_headless():
    seen: list[str] = []
    endpoint = "https://race.netkeiba.com/api/api_get_race_result.html?race_id=202406050811"
    settled, client = _settled(
        _http_error(403),
        routes={endpoint: _runners_json([("1", 1, "イチ")])},
        headless=lambda url: seen.append(url) or "<html></html>",
    )
    assert settled.results == []
    assert settled.payouts == []
    assert settled.race_name is None
    assert settled.complete is False
    assert settled.result_phase is None
    assert settled.source is None
    assert settled.warning == RACE_RESULT_FORBIDDEN
    assert seen == []
    assert [call["url"] for call in client.calls] == [RESULT_URL]


def test_login_wall_stops_before_the_fallback():
    endpoint = "https://race.netkeiba.com/api/api_get_race_result.html?race_id=202406050811"
    html = f"""<html><head><title>ログイン</title></head><body>
    <form><input type="password" name="pswd"></form>
    <script>$.ajax({{url: '{endpoint}'}});</script>
    </body></html>"""
    settled, client = _settled(html, routes={endpoint: _runners_json([("1", 1, "イチ")])})
    assert settled.warning == RACE_RESULT_LOGIN
    assert settled.results == []
    assert settled.complete is False
    assert [call["url"] for call in client.calls] == [RESULT_URL]


def test_jra_payout_mismatch_is_a_warning_and_does_not_replace_the_price():
    umaren = """<tr class="Umaren"><th>馬連</th>
<td class="Result"><div><span>1-2</span></div></td>
<td class="Payout"><span>300円</span></td>
<td class="Ninki"><span>2人気</span></td></tr>"""
    html = _result_html(
        [_row("1", 1, "イチ")],
        heads=1,
        phase="確定",
        extra=_WIN_PAYOUT.replace("</table>", umaren + "</table>"),
    )
    settled, client = _settled(
        html,
        posts={
            "pw01hli00/03": _JRA_INDEX,
            _JRA_CNAME: _jra_day("200", umaren="300"),
        },
    )
    assert settled.payouts[0].payout == 100
    assert settled.payouts[0].source == "netkeiba_html"
    assert settled.payouts[0].fetched_at == FETCHED_AT
    assert settled.warning is not None
    assert settled.warning.startswith(JRA_PAYOUT_MISMATCH)
    assert "100円" in settled.warning
    assert "200円" in settled.warning
    assert client.calls[-1]["params"]["cname"] == _JRA_CNAME


def test_jra_payout_match_adds_no_warning():
    html = _result_html([_row("1", 1, "イチ")], heads=1, phase="確定", extra=_WIN_PAYOUT)
    settled, _client = _settled(
        html,
        posts={"pw01hli00/03": _JRA_INDEX, _JRA_CNAME: _jra_day("100")},
    )
    assert settled.warning is None
    assert settled.payouts[0].payout == 100
    assert settled.result_phase == "confirmed"
    assert settled.complete is True


def test_jra_payout_403_stops_and_keeps_the_netkeiba_price():
    html = _result_html([_row("1", 1, "イチ")], heads=1, phase="確定", extra=_WIN_PAYOUT)
    settled, client = _settled(html, posts={"pw01hli00/03": _http_error(403)})
    assert settled.payouts[0].payout == 100
    assert settled.warning == JRA_PAYOUT_FORBIDDEN
    assert all(call.get("params", {}).get("cname") != _JRA_CNAME for call in client.calls)


def test_pre_race_models_do_not_carry_result_fields():
    banned = {"status", "status_label", "demoted", "complete", "result_phase"}
    for model in (Race, RaceEntry, Horse):
        assert banned.isdisjoint(model.model_fields)


@pytest.mark.skipif(not headless_browser_installed(), reason="headless browser is not installed")
def test_installed_headless_browser_renders_a_local_result_page(tmp_path: Path):
    page = tmp_path / "result.html"
    page.write_text(
        _result_html([_row("取消", 1, "イチ")], heads=1, phase="確定"), encoding="utf-8"
    )
    rendered = render_headless(page.as_uri())
    assert "取消" in rendered
    assert "イチ" in rendered


@pytest.mark.integration
def test_live_arima_kinen_2024_result():
    """Live check against the settled 2024 Arima Kinen result page."""
    with NetkeibaScraper() as scraper:
        settled = scraper.get_settled_race_result(RACE_ID)

    warning = settled.warning or ""
    assert "race result fetch failed" not in warning
    assert "race result returned 403" not in warning
    assert "JRA payout mismatch" not in warning
    assert "race result incomplete" not in warning
    if settled.result_phase is None:
        assert "result phase was not on the page" in warning
    else:
        assert settled.result_phase in {"provisional", "confirmed"}
    assert settled.race_name == "有馬記念"
    assert settled.race_date == date(2024, 12, 22)
    assert settled.venue == "中山"
    assert settled.distance == 2500
    assert settled.track_type == "芝"
    assert settled.track_condition == "良"
    assert settled.results[0].horse_name == "レガレイラ"
    assert settled.results[0].horse_number == 8
    assert settled.results[0].rank == 1
    assert settled.results[0].finish_time == "2:31.8"
    assert settled.results[0].odds == 10.9
    assert settled.results[-1].horse_name == "ドウデュース"
    assert settled.results[-1].rank is None
    assert settled.results[-1].status == "scratched"
    assert settled.results[-1].status_label == "取消"
    assert settled.complete is True
    assert settled.results[0].source == "netkeiba_html"
    assert settled.results[0].fetched_at is not None
    win = next(payout for payout in settled.payouts if payout.bet_type == "単勝")
    assert win.combination == "8"
    assert win.payout == 1090
    assert win.popularity == 4
