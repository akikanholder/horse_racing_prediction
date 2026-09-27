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
from backend.scrapers.netkeiba_scraper import (
    RACE_RESULT_EMPTY,
    RACE_RESULT_FETCH_FAILED,
    RACE_RESULT_NOT_FOUND,
    RACE_RESULT_NOT_SETTLED,
    RACE_RESULT_PAYOUT_BLANK,
    RACE_RESULT_PAYOUT_MISSING,
    RACE_RESULT_PAYOUT_UNPAIRED,
    RACE_RESULT_TABLE_MALFORMED,
    NetkeibaScraper,
)
from backend.services.race_service import RaceService

FIXTURES = Path(__file__).parent / "fixtures"
RACE_ID = "202406050811"
FETCHED_AT = datetime(2026, 9, 27, 12, 0, 0)
RESULT_URL = "https://race.netkeiba.com/race/result.html"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class _FakeHTTP:
    def __init__(self, page: str | Exception):
        self.page = page
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, encoding=None):
        self.calls.append(
            {"url": url, "params": params, "headers": headers or {}, "encoding": encoding}
        )
        if isinstance(self.page, Exception):
            raise self.page
        return self.page

    def close(self) -> None:
        return None


def _scraper(page: str | Exception) -> tuple[NetkeibaScraper, _FakeHTTP]:
    client = _FakeHTTP(page)
    return NetkeibaScraper(http_client=client), client  # type: ignore[arg-type]


def _settled(page: str | Exception, race_id: str = RACE_ID):
    scraper, client = _scraper(page)
    return scraper.get_settled_race_result(race_id, fetched_at=FETCHED_AT), client


def test_arima_kinen_finish_and_payouts_come_from_the_result_page():
    settled, client = _settled(_load("netkeiba_race_result.html"))

    assert settled.warning is None
    assert settled.payout_note is None
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
    assert body["warning"] is None
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
    }


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


@pytest.mark.integration
def test_live_arima_kinen_2024_result():
    """Live check against the settled 2024 Arima Kinen result page."""
    with NetkeibaScraper() as scraper:
        settled = scraper.get_settled_race_result(RACE_ID)

    assert settled.warning is None
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
    win = next(payout for payout in settled.payouts if payout.bet_type == "単勝")
    assert win.combination == "8"
    assert win.payout == 1090
    assert win.popularity == 4
