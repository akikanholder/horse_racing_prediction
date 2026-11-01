"""Ops-review fixes: full order, fresh going, and trends that can stop early."""

import time
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from backend.models.race import Race, SettledRaceResult
from backend.scrapers.day_of_bias import BiasLabels, assemble_publication
from backend.scrapers.jra_track_scraper import (
    GOING_NOT_RACE_DATE,
    JRATrackScraper,
    _calendar_day,
)
from backend.scrapers.netkeiba_scraper import NetkeibaScraper
from backend.scrapers.page_cache import CachingHTTPClient
from backend.scrapers.page_fallback import AccessBlocked, same_site_result_urls
from backend.services.historical_service import HistoricalService, _transient_failure

TOKYO = timezone(timedelta(hours=9))
FETCHED = datetime(2026, 11, 1, 16, 0, tzinfo=TOKYO)
DAY = date(2026, 11, 1)
RESULT_URL = "https://race.netkeiba.com/race/result.html"


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


def test_calendar_day_skips_a_31st_that_the_month_does_not_have():
    assert _calendar_day(2026, 11, 31) is None
    assert _calendar_day(2026, 11, 30) == datetime(2026, 11, 30)


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
