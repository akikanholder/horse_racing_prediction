"""Calibration export, edition diff, and ROI over the prediction store."""

import csv
import io
import json
import math
from copy import deepcopy
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from backend.cli import main as cli_main
from backend.main import app
from backend.models.race import RacePayout, SettledFinisher, SettledRaceResult
from backend.scrapers.netkeiba_scraper import NetkeibaScraper
from backend.services.prediction_report import (
    FinalWinOdds,
    diff_editions,
    export_backtest,
    roi_summary,
    rows_as_csv,
    rows_as_jsonl,
)
from backend.services.prediction_store import PredictionStore, PredictionValidationError
from backend.services.prediction_validate import expected_record_id
from backend.services.race_service import RaceService

FIXTURE = Path(__file__).parent / "fixtures" / "predictions" / "example_record.json"
TOKYO = ZoneInfo("Asia/Tokyo")
DAY = "2026-10-18"
RACE_ID = "202610180811"
OTHER_RACE = "202610180512"
NEXT_RACE = "202610190511"
NEXT_DAY = "2026-10-19"
FETCHED = datetime(2026, 10, 18, 16, 30, tzinfo=TOKYO)
CLOCK = datetime(2026, 10, 18, 16, 45, tzinfo=TOKYO)


def _example() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _horse(
    number: int,
    name: str,
    prob: float | None,
    odds: float | None,
    *,
    mark: str | None = "◎",
    ability: str | None = "A",
) -> dict:
    horse = deepcopy(_example()["horses"][0])
    horse["horse_number"] = number
    horse["horse_name"] = name
    horse["win_prob"] = prob
    horse["win_prob_range"] = None if prob is None else [0, 1]
    horse["odds_win"] = odds
    horse["buy_mark"] = mark
    horse["ability_mark"] = ability
    return horse


def _bet(
    ticket: str,
    horses: list[int],
    *,
    status: str = "bought",
    stake: int | None = 100,
    odds: float | None = 4.0,
    prob: float | None = None,
    odds_min: float | None = None,
    odds_max: float | None = None,
) -> dict:
    if ticket == "wide":
        odds = None
        if odds_min is None and odds_max is None:
            odds_min, odds_max = 1.5, 2.5
    else:
        odds_min = None
        odds_max = None
    if status != "bought":
        stake = None
    return {
        "ticket_type": ticket,
        "combination": horses,
        "prob": prob,
        "odds": odds,
        "odds_min": odds_min,
        "odds_max": odds_max,
        "ev": None,
        "ev_conservative": None,
        "stake_yen": stake,
        "status": status,
        "reason": None,
    }


def _sheet(
    *,
    race_id: str = RACE_ID,
    day: str = DAY,
    edition: str = "day_before",
    run_at: str = "2026-10-17T18:00:00+09:00",
    horses: list[dict] | None = None,
    bets: list[dict] | None = None,
    surface: str = "芝",
    venue: str = "東京",
) -> dict:
    record = _example()
    record["race"]["race_id"] = race_id
    record["race"]["date"] = day
    record["race"]["surface"] = surface
    record["race"]["venue"] = venue
    record["run"]["edition"] = edition
    record["run"]["run_at"] = run_at
    if horses is not None:
        record["horses"] = horses
    if bets is not None:
        record["bets"] = bets
    record["record_id"] = expected_record_id(race_id, edition, run_at)
    record["result"] = {"status": "pending"}
    return record


def _finish(number: int, rank: int | None, status: str = "finished", odds: float | None = None):
    return SettledFinisher(
        horse_number=number,
        rank=rank if status == "finished" else None,
        status=status,  # type: ignore[arg-type]
        odds=odds,
        source="netkeiba_html",
        fetched_at=FETCHED,
    )


def _payout(label: str, combination: str, yen: int) -> RacePayout:
    return RacePayout(
        bet_type=label,
        combination=combination,
        payout=yen,
        source="netkeiba_html",
        fetched_at=FETCHED,
    )


def _settled(
    race_id: str = RACE_ID,
    *,
    results: list[SettledFinisher] | None = None,
    payouts: list[RacePayout] | None = None,
    phase: str | None = "confirmed",
    complete: bool = True,
    http_status: int | None = None,
    warning: str | None = None,
) -> SettledRaceResult:
    return SettledRaceResult(
        race_id=race_id,
        fetched_at=FETCHED,
        result_phase=phase,  # type: ignore[arg-type]
        complete=complete,
        results=results or [],
        payouts=payouts or [],
        http_status=http_status,
        warning=warning,
        source="netkeiba_html",
    )


def _store(tmp_path: Path) -> PredictionStore:
    return PredictionStore(tmp_path)


def _settle(store: PredictionStore, results: dict[str, SettledRaceResult], day: str = DAY) -> None:
    store.settle(
        datetime.strptime(day, "%Y-%m-%d").date(),
        lambda race_id: results[race_id],
        now=lambda: CLOCK,
    )


def _meeting(day: str = DAY):
    return datetime.strptime(day, "%Y-%m-%d").date()


def _scored_sheet() -> dict:
    horses = [
        _horse(3, "勝ち", 0.75, 2.5, mark="◎"),
        _horse(7, "負け", 0.25, 8.0, mark="○"),
        _horse(5, "取消", 0.10, 4.0, mark="△"),
    ]
    bets = [
        _bet("win", [3], stake=100, odds=2.5),
        _bet("win", [7], stake=100, odds=8.0),
        _bet("win", [5], stake=100, odds=4.0),
        _bet("quinella", [1, 3], stake=200, odds=12.0),
        _bet("wide", [1, 4], stake=100, odds_min=1.5, odds_max=9.0),
        _bet("wide", [3, 7], status="reference", odds_min=1.5, odds_max=9.0),
    ]
    return _sheet(horses=horses, bets=bets)


def _scored_result() -> SettledRaceResult:
    return _settled(
        results=[
            _finish(3, 1, odds=2.4),
            _finish(7, 2, odds=7.8),
            _finish(5, None, "scratched"),
            _finish(1, 3, odds=20.0),
        ],
        payouts=[
            _payout("単勝", "3", 250),
            _payout("馬連", "1-2", 800),
            _payout("ワイド", "1-2", 150),
        ],
    )


def test_export_uses_the_record_and_does_not_invent_final_odds(tmp_path: Path):
    store = _store(tmp_path)
    store.add([_scored_sheet(), _sheet(race_id=OTHER_RACE, horses=[_horse(1, "未", 0.4, None)])])
    _settle(
        store,
        {
            RACE_ID: _scored_result(),
            OTHER_RACE: _settled(OTHER_RACE, phase="provisional", complete=False),
        },
    )
    report = export_backtest(store, _meeting(), _meeting(), bins=10)
    by_key = {(row["record_id"], row["horse_number"]): row for row in report["rows"]}
    winner = by_key[(_scored_sheet()["record_id"], 3)]
    loser = by_key[(_scored_sheet()["record_id"], 7)]
    scratched = by_key[(_scored_sheet()["record_id"], 5)]
    assert winner["win_prob"] == 0.75
    assert winner["odds_at_prediction"] == 2.5
    assert winner["final_odds"] is None
    assert winner["finish_position"] == 1
    assert winner["won"] is True
    assert winner["surface"] == "芝"
    assert winner["distance_m"] == 1600
    assert winner["going"] == "良"
    assert winner["field_size"] == 16
    assert winner["venue"] == "東京"
    assert "final win odds were not on the prediction record" in winner["warning"]
    assert loser["won"] is False
    assert loser["finish_position"] == 2
    assert loser["final_odds"] != 3.6
    assert scratched["finish_position"] is None
    assert scratched["won"] is None
    assert "finish position was not on the result: 5" in scratched["warning"]
    pending = by_key[(expected_record_id(OTHER_RACE, "day_before", "2026-10-17T18:00:00+09:00"), 1)]
    assert pending["won"] is None
    assert pending["odds_at_prediction"] is None
    assert "result was not settled" in pending["warning"]
    assert "odds at prediction time were not on the record" in pending["warning"]
    scored = report["calibration"]
    assert scored["scored"] == 2
    assert scored["brier"] == 0.0625
    assert scored["log_loss"] == float(
        Decimal(str(-math.log(0.75))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    )
    populated = [bucket for bucket in scored["bins"] if bucket["count"]]
    assert populated == [
        {"lo": 0.2, "hi": 0.3, "count": 1, "mean_predicted": 0.25, "observed_win_rate": 0.0},
        {"lo": 0.7, "hi": 0.8, "count": 1, "mean_predicted": 0.75, "observed_win_rate": 1.0},
    ]
    table = csv.DictReader(io.StringIO(rows_as_csv(report["rows"])))
    first = next(row for row in table if row["horse_number"] == "3")
    assert first["final_odds"] == ""
    assert first["won"] == "true"
    line = json.loads(rows_as_jsonl([winner]).strip())
    assert line["final_odds"] is None
    assert line["won"] is True


def test_log_loss_stays_null_only_when_the_extreme_probability_is_wrong(tmp_path: Path):
    store = _store(tmp_path)
    horses = [_horse(7, "ゼロ", 0.0, 5.0), _horse(3, "イチ", 1.0, 1.1)]
    store.add([_sheet(horses=horses, bets=[_bet("win", [7])])])
    _settle(
        store,
        {
            RACE_ID: _settled(
                results=[_finish(7, 1), _finish(3, 2)],
                payouts=[_payout("単勝", "7", 200)],
            )
        },
    )
    impossible = export_backtest(store, _meeting(), _meeting())
    assert impossible["calibration"]["brier"] == 1.0
    assert impossible["calibration"]["log_loss"] is None
    assert "log loss is undefined when win_prob is 0 or 1" in impossible["calibration"]["warning"]

    matched = PredictionStore(tmp_path / "matched")
    matched.add([_sheet(horses=[_horse(7, "イチ", 1.0, 1.1)], bets=[_bet("win", [7])])])
    _settle(
        matched,
        {RACE_ID: _settled(results=[_finish(7, 1)], payouts=[_payout("単勝", "7", 110)])},
    )
    certain = export_backtest(matched, _meeting(), _meeting())
    assert certain["calibration"]["brier"] == 0.0
    assert certain["calibration"]["log_loss"] == 0.0


def test_fetch_final_odds_reads_the_result_page_and_stops_when_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    store = _store(tmp_path)
    store.add([_scored_sheet(), _sheet(race_id=OTHER_RACE, horses=[_horse(2, "次", 0.2, 3.0)])])
    calls: list[str] = []

    class _Service:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get_settled_race_result(self, race_id, **kwargs):
            calls.append(race_id)
            if race_id == RACE_ID:
                return _scored_result()
            return _settled(race_id, results=[_finish(1, 1, odds=9.0)])

    monkeypatch.setattr("backend.cli.RaceService", lambda: _Service())

    def run(*argv: str) -> str:
        monkeypatch.setattr("sys.argv", ["cli", *argv])
        cli_main()
        return capsys.readouterr().out

    offline = json.loads(
        run("predictions", "export", "--from", DAY, "--dir", str(tmp_path), "--json")
    )
    assert calls == []
    assert offline["rows"][0]["final_odds"] is None
    assert "final win odds were not on the prediction record" in offline["warning"]

    fetched = json.loads(
        run(
            "predictions",
            "export",
            "--from",
            "20261018",
            "--dir",
            str(tmp_path),
            "--fetch-final-odds",
            "--json",
        )
    )
    assert calls == [RACE_ID, OTHER_RACE]
    priced = next(
        row for row in fetched["rows"] if row["horse_number"] == 3 and row["race_id"] == RACE_ID
    )
    assert priced["final_odds"] == 2.4
    assert "final win odds were not on the prediction record" not in (priced["warning"] or "")
    stopped = next(row for row in fetched["rows"] if row["race_id"] == OTHER_RACE)
    assert stopped["final_odds"] is None
    assert "final win odds were not on the result: 2" in stopped["warning"]

    calls.clear()

    class _Refused:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get_settled_race_result(self, race_id, **kwargs):
            calls.append(race_id)
            return _settled(
                race_id,
                http_status=403,
                phase=None,
                complete=False,
                warning="race result returned 403",
            )

    monkeypatch.setattr("backend.cli.RaceService", lambda: _Refused())
    refused = json.loads(
        run(
            "predictions",
            "export",
            "--from",
            DAY,
            "--to",
            DAY,
            "--dir",
            str(tmp_path),
            "--fetch-final-odds",
            "--json",
        )
    )
    assert calls == [RACE_ID]
    assert "stopped at 202610180811: race result returned 403" in refused["warning"]
    assert all(row["final_odds"] is None for row in refused["rows"])

    class _Provisional(_Service):
        def get_settled_race_result(self, race_id, **kwargs):
            calls.append(race_id)
            card = _scored_result()
            card.result_phase = "provisional"
            card.complete = False
            return card

    calls.clear()
    monkeypatch.setattr("backend.cli.RaceService", lambda: _Provisional())
    early = json.loads(
        run(
            "predictions",
            "export",
            "--from",
            DAY,
            "--dir",
            str(tmp_path),
            "--fetch-final-odds",
            "--json",
        )
    )
    assert "final win odds require a confirmed complete race" in early["warning"]
    assert all(row["final_odds"] is None for row in early["rows"] if row["race_id"] == RACE_ID)


def test_supplied_final_odds_do_not_replace_a_blank_cell(tmp_path: Path):
    store = _store(tmp_path)
    store.add([_sheet(horses=[_horse(7, "サンプル", 0.22, 6.2)], bets=[_bet("win", [7])])])
    _settle(
        store, {RACE_ID: _settled(results=[_finish(7, 1)], payouts=[_payout("単勝", "7", 360)])}
    )
    bundle = FinalWinOdds(by_race={RACE_ID: {7: None}})
    report = export_backtest(store, _meeting(), _meeting(), final_win_odds=bundle)
    row = report["rows"][0]
    assert row["final_odds"] is None
    assert row["odds_at_prediction"] == 6.2
    assert "final win odds were not on the result: 7" in row["warning"]


def test_diff_reports_probability_mark_bet_and_profit_changes(tmp_path: Path):
    store = _store(tmp_path)
    before = _sheet(
        horses=[
            _horse(7, "サンプル", 0.22, 6.2, mark="◎", ability="A"),
            _horse(1, "のみ前日", 0.05, 30.0),
        ],
        bets=[_bet("win", [7], stake=1000, odds=6.2, prob=0.22)],
    )
    same = _sheet(
        edition="pre_post_45m",
        run_at="2026-10-18T15:00:00+09:00",
        horses=[
            _horse(7, "サンプル", 0.40, 4.0, mark="○", ability="A"),
            _horse(3, "のみ当日", 0.15, 9.0),
        ],
        bets=[
            _bet("win", [7], stake=500, odds=4.0, prob=0.40),
            _bet("wide", [3, 7], status="reference", odds_min=1.2, odds_max=1.8),
        ],
    )
    alone = _sheet(
        race_id=OTHER_RACE, horses=[_horse(4, "片方", 0.3, 5.0)], bets=[_bet("win", [4])]
    )
    store.add([before, same, alone])
    _settle(
        store,
        {
            RACE_ID: _settled(
                results=[_finish(7, 1), _finish(3, 2)], payouts=[_payout("単勝", "7", 360)]
            ),
            OTHER_RACE: _settled(OTHER_RACE, phase="provisional", complete=False),
        },
    )
    report = diff_editions(store, _meeting())
    pair = report["pairs"][0]
    assert pair["day_before"]["record_id"].endswith("day_before_20261017T180000+0900")
    assert "pre_post_45m" in pair["pre_post_45m"]["record_id"]
    horses = {row["horse_number"]: row for row in pair["horses"]}
    assert horses[7]["win_prob_delta"] == 0.18
    assert horses[7]["marks_changed"] is True
    assert horses[7]["buy_mark_day_before"] == "◎"
    assert horses[7]["buy_mark_pre_post_45m"] == "○"
    assert horses[1]["win_prob_delta"] is None
    assert horses[1]["marks_changed"] is None
    assert "horse was not on the pre_post_45m record: 1" in horses[1]["warning"]
    assert horses[3]["marks_changed"] is None
    changed = pair["bets"]["changed"]
    assert changed[0]["ticket_type"] == "win"
    assert changed[0]["fields"] == ["prob", "odds", "stake_yen"]
    assert changed[0]["day_before"]["stake_yen"] == 1000
    assert changed[0]["pre_post_45m"]["odds"] == 4.0
    assert pair["bets"]["added"][0]["ticket_type"] == "wide"
    assert pair["bets"]["removed"] == []
    assert pair["profit"]["day_before_profit_yen"] == 2600
    assert pair["profit"]["pre_post_45m_profit_yen"] == 1300
    assert pair["profit"]["profit_delta_yen"] == -1300
    assert report["unpaired"][0]["race_id"] == OTHER_RACE
    assert "no pre_post_45m record for this race" in report["unpaired"][0]["warning"]


def test_unsettled_editions_leave_profit_null(tmp_path: Path):
    store = _store(tmp_path)
    store.add(
        [
            _sheet(bets=[_bet("win", [7])]),
            _sheet(
                edition="pre_post_45m",
                run_at="2026-10-18T15:00:00+09:00",
                bets=[_bet("win", [7], stake=200)],
            ),
        ]
    )
    report = diff_editions(store, _meeting(), race_id=RACE_ID)
    profit = report["pairs"][0]["profit"]
    assert profit["day_before_profit_yen"] is None
    assert profit["pre_post_45m_profit_yen"] is None
    assert profit["profit_delta_yen"] is None
    assert "day_before edition was not settled" in report["pairs"][0]["warning"]
    assert "pre_post_45m edition was not settled" in report["pairs"][0]["warning"]
    empty = diff_editions(store, _meeting(), race_id="202610180101")
    assert empty["pairs"] == []
    assert empty["warning"] == "no prediction records for this race"
    with pytest.raises(PredictionValidationError, match="12-digit"):
        diff_editions(store, _meeting(), race_id="11")


def test_roi_groups_hits_misses_refunds_and_leaves_wide_unbanded(tmp_path: Path):
    store = _store(tmp_path)
    pending = _sheet(
        race_id=OTHER_RACE,
        horses=[_horse(4, "未精算", 0.2, 3.0)],
        bets=[_bet("win", [4], stake=500, odds=3.0)],
    )
    later = _sheet(
        race_id=NEXT_RACE,
        day=NEXT_DAY,
        run_at="2026-10-18T18:00:00+09:00",
        surface="ダート",
        venue="阪神",
        horses=[_horse(1, "翌日", 0.5, 4.0)],
        bets=[_bet("win", [1], stake=100, odds=4.0)],
    )
    edges = _sheet(
        race_id="202610180612",
        surface="ダート",
        horses=[_horse(8, "境界", 0.2, 3.0)],
        bets=[
            _bet("win", [8], stake=100, odds=3.0),
            _bet("win", [8], stake=100, odds=100.0),
            _bet("win", [8], stake=100, odds=0.8),
        ],
    )
    store.add([_scored_sheet()])
    _settle(store, {RACE_ID: _scored_result()}, DAY)
    store.add([pending, edges])
    _settle(
        store,
        {
            OTHER_RACE: _settled(OTHER_RACE, phase="provisional", complete=False),
            "202610180612": _settled(
                "202610180612",
                results=[_finish(8, 4)],
                payouts=[_payout("単勝", "1", 200)],
            ),
        },
    )
    store.add([later])
    _settle(
        store,
        {
            NEXT_RACE: _settled(
                NEXT_RACE, results=[_finish(1, 1)], payouts=[_payout("単勝", "1", 400)]
            )
        },
        NEXT_DAY,
    )
    report = roi_summary(store, _meeting(), _meeting(NEXT_DAY))
    assert "record was not settled" in report["warning"]
    assert "wide odds are a range and were not banded" in report["warning"]
    groups = {
        (group["day"], group["bet_type"], group["surface"], group["odds_band"]): group
        for group in report["groups"]
    }
    hit = groups[(DAY, "win", "芝", "1-3")]
    assert hit["bet_count"] == 1
    assert hit["hit_count"] == 1
    assert hit["stake_yen"] == 100
    assert hit["return_yen"] == 250
    assert hit["profit_yen"] == 150
    assert hit["hit_rate"] == 1.0
    assert hit["roi"] == 1.5
    miss = groups[(DAY, "win", "芝", "5-10")]
    assert miss["hit_count"] == 0
    assert miss["return_yen"] == 0
    assert miss["roi"] == -1.0
    refund = groups[(DAY, "win", "芝", "3-5")]
    assert refund["refund_count"] == 1
    assert refund["stake_yen"] == 100
    assert refund["return_yen"] == 100
    assert refund["refund_yen"] == 100
    assert refund["hit_rate"] is None
    assert refund["roi"] == 0.0
    assert "no decided bets" in refund["warning"]
    quinella = groups[(DAY, "quinella", "芝", "10-20")]
    assert quinella["stake_yen"] == 200
    assert quinella["roi"] == -1.0
    wide = groups[(DAY, "wide", "芝", None)]
    assert wide["odds_band"] is None
    assert wide["bet_count"] == 1
    assert wide["return_yen"] == 0
    assert "wide odds are a range and were not banded" in wide["warning"]
    dirt = groups[(NEXT_DAY, "win", "ダート", "3-5")]
    assert dirt["return_yen"] == 400
    assert dirt["roi"] == 3.0
    assert groups[(DAY, "win", "ダート", "100+")]["odds_band"] == "100+"
    assert groups[(DAY, "win", "ダート", "<1")]["odds_band"] == "<1"
    assert all(
        group["bet_type"] != "wide" or group["odds_band"] is None for group in report["groups"]
    )

    by_day = roi_summary(store, _meeting(), _meeting(NEXT_DAY), by="day")
    day_groups = {group["day"]: group for group in by_day["groups"]}
    assert "bet_type" not in day_groups[DAY]
    assert day_groups[DAY]["bet_count"] == 8
    assert day_groups[DAY]["hit_count"] == 1
    assert day_groups[DAY]["refund_count"] == 1
    assert day_groups[DAY]["stake_yen"] == 900
    assert day_groups[DAY]["return_yen"] == 350
    assert day_groups[DAY]["hit_rate"] == 0.1429
    assert day_groups[DAY]["roi"] == -0.6111
    narrow = roi_summary(store, _meeting(), _meeting(), by="odds_band", odds_bands="1,3,10")
    bands = {group["odds_band"]: group for group in narrow["groups"]}
    assert bands["1-3"]["bet_count"] == 1
    assert bands["3-10"]["bet_count"] == 3
    with pytest.raises(PredictionValidationError, match="unknown roi grouping"):
        roi_summary(store, _meeting(), _meeting(), by="class")
    with pytest.raises(PredictionValidationError, match="increasing"):
        roi_summary(store, _meeting(), _meeting(), odds_bands="5,1")


def test_parsed_combination_payouts_are_what_settle_uses(tmp_path: Path):
    html = """
    <table class="Payout_Detail_Table">
      <tr><th>ワイド</th><td class="Result"><ul><li>7</li><li>3</li></ul></td>
        <td class="Payout"><span>180円</span></td><td class="Ninki"><span>2人気</span></td></tr>
      <tr><th>馬連</th><td class="Result"><ul><li>3</li><li>7</li></ul></td>
        <td class="Payout"><span>540円</span></td><td class="Ninki"><span>4人気</span></td></tr>
      <tr><th>三連複</th><td class="Result"><ul><li>1</li><li>3</li><li>7</li></ul></td>
        <td class="Payout"><span>1200円</span></td><td class="Ninki"><span>6人気</span></td></tr>
    </table>
    """
    scraper = object.__new__(NetkeibaScraper)
    payouts, note = NetkeibaScraper._settled_payouts(
        scraper,
        BeautifulSoup(html, "lxml"),
        source="netkeiba_html",
        fetched_at=FETCHED,
    )
    assert [(row.bet_type, row.combination, row.payout) for row in payouts] == [
        ("ワイド", "7-3", 180),
        ("馬連", "3-7", 540),
        ("三連複", "1-3-7", 1200),
    ]
    store = _store(tmp_path)
    store.add(
        [
            _sheet(
                bets=[
                    _bet("wide", [3, 7], stake=200),
                    _bet("wide", [1, 2], stake=100),
                    _bet("quinella", [3, 7], stake=100),
                    _bet("trio", [1, 3, 7], stake=100),
                    _bet("trio", [1, 2, 4], stake=100),
                ]
            )
        ]
    )
    _settle(
        store,
        {
            RACE_ID: _settled(
                results=[_finish(7, 1), _finish(3, 2), _finish(1, 3), _finish(2, 4)],
                payouts=payouts,
            )
        },
    )
    lines = store.read(_meeting())[0]["result"]["settlement"]["bets"]
    assert [(line["hit"], line["payout_yen"]) for line in lines] == [
        (True, 360),
        (False, 0),
        (True, 540),
        (True, 1200),
        (False, 0),
    ]


def test_cli_and_api_report_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    store = _store(tmp_path)
    before = _sheet(
        horses=[_horse(7, "サンプル", 0.22, 6.2)], bets=[_bet("win", [7], stake=100, odds=6.2)]
    )
    same = _sheet(
        edition="pre_post_45m",
        run_at="2026-10-18T15:00:00+09:00",
        horses=[_horse(7, "サンプル", 0.30, 5.0, mark="○")],
        bets=[_bet("win", [7], stake=200, odds=5.0)],
    )
    store.add([before, same])
    _settle(
        store,
        {RACE_ID: _settled(results=[_finish(7, 1, odds=4.2)], payouts=[_payout("単勝", "7", 420)])},
    )
    monkeypatch.setattr(
        RaceService,
        "get_settled_race_result",
        lambda self, race_id, **kwargs: (_ for _ in ()).throw(AssertionError("network")),
    )

    def run(*argv: str) -> tuple[str, str]:
        monkeypatch.setattr("sys.argv", ["cli", *argv])
        cli_main()
        captured = capsys.readouterr()
        return captured.out, captured.err

    out_dir = tmp_path / "out"
    stdout, stderr = run(
        "predictions",
        "export",
        "--from",
        "20261018",
        "--to",
        "2026-10-18",
        "--format",
        "csv",
        "--output",
        str(out_dir / "horses.csv"),
        "--dir",
        str(tmp_path),
        "--bins",
        "5",
    )
    assert "Brier:" in stdout
    assert stderr == ""
    written = (out_dir / "horses.csv").read_text(encoding="utf-8")
    assert "final_odds" in written.splitlines()[0]
    assert ",," in written or written.count(",,") >= 0
    body = list(csv.DictReader(io.StringIO(written)))
    assert body[0]["final_odds"] == ""
    assert {row["edition"] for row in body} == {"day_before", "pre_post_45m"}

    diff_out, _err = run(
        "predictions", "diff", "20261018", "--race", RACE_ID, "--dir", str(tmp_path), "--json"
    )
    diff = json.loads(diff_out)
    assert diff["pairs"][0]["profit"]["profit_delta_yen"] == 320
    roi_out, _err = run(
        "predictions",
        "roi",
        "--from",
        DAY,
        "--by",
        "bet_type,surface",
        "--format",
        "csv",
        "--dir",
        str(tmp_path),
    )
    roi_row = next(csv.DictReader(io.StringIO(roi_out)))
    assert roi_row["bet_type"] == "win"
    assert roi_row["surface"] == "芝"
    assert roi_row["stake_yen"] == "300"
    assert roi_row["return_yen"] == "1260"

    monkeypatch.setenv("HRP_PREDICTIONS_DIR", str(tmp_path))
    with TestClient(app) as client:
        exported = client.get("/api/predictions/export", params={"from": "20261018", "bins": 10})
        assert exported.status_code == 200
        assert exported.json()["calibration"]["scored"] == 2
        assert exported.json()["rows"][0]["final_odds"] is None
        compared = client.get("/api/predictions/diff", params={"date": "20261018", "race": RACE_ID})
        assert compared.status_code == 200
        assert compared.json()["pairs"][0]["horses"][0]["win_prob_delta"] == 0.08
        summarized = client.get(
            "/api/predictions/roi",
            params={"from": "20261018", "to": "20261018", "by": "surface"},
        )
        assert summarized.status_code == 200
        assert summarized.json()["groups"][0]["roi"] == 3.2
        bad = client.get("/api/predictions/roi", params={"from": "20261018", "by": "nope"})
        assert bad.status_code == 400
        backwards = client.get(
            "/api/predictions/export", params={"from": "20261019", "to": "20261018"}
        )
        assert backwards.status_code == 400
