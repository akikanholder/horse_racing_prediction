"""Prediction JSONL store: validate, append, and settle from confirmed results."""

import json
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from backend.cli import main as cli_main
from backend.main import app
from backend.models.race import RacePayout, RaceRefund, SettledFinisher, SettledRaceResult
from backend.services.prediction_store import (
    DuplicateRecordError,
    PredictionStore,
    PredictionStoreError,
    PredictionValidationError,
)
from backend.services.prediction_validate import (
    expected_record_id,
    prediction_schema,
    schema_path,
    validate_prediction,
)
from backend.services.race_service import RaceService

FIXTURE = Path(__file__).parent / "fixtures" / "predictions" / "example_record.json"
TOKYO = ZoneInfo("Asia/Tokyo")
DAY = "2026-10-18"
RACE_ID = "202610180811"
FETCHED = datetime(2026, 10, 18, 16, 30, tzinfo=TOKYO)
CLOCK = datetime(2026, 10, 18, 16, 45, tzinfo=TOKYO)


def _example() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _bet(
    ticket: str,
    horses: list[int],
    *,
    status: str = "bought",
    stake: int | None = 100,
    odds: float | None = 4.0,
    odds_min: float | None = None,
    odds_max: float | None = None,
) -> dict:
    if ticket == "wide":
        odds = None
        if odds_min is None and odds_max is None:
            odds_min, odds_max = 1.5, 2.5
    elif status == "bought":
        odds_min = None
        odds_max = None
    if status != "bought" and stake == 100:
        stake = None
    return {
        "ticket_type": ticket,
        "combination": horses,
        "prob": None,
        "odds": odds,
        "odds_min": odds_min,
        "odds_max": odds_max,
        "ev": None,
        "ev_conservative": None,
        "stake_yen": stake,
        "status": status,
        "reason": None,
    }


def _with_bets(bets: list[dict], **changes: object) -> dict:
    record = _example()
    record["bets"] = bets
    if "edition" in changes:
        record["run"]["edition"] = changes["edition"]
    if "run_at" in changes:
        record["run"]["run_at"] = changes["run_at"]
    if "race_id" in changes:
        record["race"]["race_id"] = changes["race_id"]
    record["record_id"] = expected_record_id(
        record["race"]["race_id"],
        record["run"]["edition"],
        record["run"]["run_at"],
    )
    return record


def _finish(number: int, rank: int | None, status: str = "finished") -> SettledFinisher:
    return SettledFinisher(
        horse_number=number,
        rank=rank if status == "finished" else None,
        status=status,  # type: ignore[arg-type]
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
    *,
    race_id: str = RACE_ID,
    phase: str | None = "confirmed",
    complete: bool = True,
    results: list[SettledFinisher] | None = None,
    payouts: list[RacePayout] | None = None,
    refunds: list[RaceRefund] | None = None,
    warning: str | None = None,
    http_status: int | None = None,
) -> SettledRaceResult:
    return SettledRaceResult(
        race_id=race_id,
        fetched_at=FETCHED,
        http_status=http_status,
        result_phase=phase,  # type: ignore[arg-type]
        complete=complete,
        results=results or [],
        payouts=payouts or [],
        refunds=refunds,
        warning=warning,
        source="netkeiba_html",
    )


def _store(tmp_path: Path) -> PredictionStore:
    return PredictionStore(tmp_path)


def _settle(store: PredictionStore, fetch, day: str = DAY):
    return store.settle(
        datetime.strptime(day, "%Y-%m-%d").date(),
        fetch,
        now=lambda: CLOCK,
    )


def _winner_card() -> SettledRaceResult:
    return _settled(
        results=[_finish(7, 1), _finish(3, 2), _finish(1, 3)],
        payouts=[_payout("単勝", "7", 360)],
    )


def _mp_add(directory: str, record: dict) -> str:
    """Child process: append one record and report ok or duplicate."""
    try:
        PredictionStore(directory).add([record])
    except DuplicateRecordError:
        return "duplicate"
    except Exception as exc:
        return f"error:{type(exc).__name__}:{exc}"
    return "ok"


def _mp_settle(directory: str, started: str, release: str, report_path: str) -> None:
    """Child process: block inside the result fetch until ``release`` exists."""
    seen: list[str] = []

    def fetch(race_id: str) -> SettledRaceResult:
        seen.append(race_id)
        Path(started).write_text(race_id, encoding="utf-8")
        deadline = time.time() + 20
        while not Path(release).exists():
            if time.time() > deadline:
                raise TimeoutError("settle fetch was not released")
            time.sleep(0.02)
        return _winner_card()

    report = PredictionStore(directory).settle(
        datetime.strptime(DAY, "%Y-%m-%d").date(),
        fetch,
        now=lambda: CLOCK,
    )
    Path(report_path).write_text(
        json.dumps({"report": report, "fetched": seen}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_example_record_is_marked_and_valid():
    raw = _example()
    assert raw["_example"] is True
    stored = validate_prediction(raw, pending_only=True)
    assert stored["_example"] is True
    assert stored["schema"] == "hrp.prediction"
    assert stored["run"]["weights"] == {
        "ability": 25,
        "pace": 15,
        "course": 15,
        "going": 12,
        "workout": 10,
        "gate_jockey": 10,
        "pedigree": 5,
        "rotation": 8,
    }
    assert stored["schema_version"] == "1.0.0"
    assert stored["run"]["rules"] == raw["run"]["rules"]


def test_schema_does_not_fix_rules_shape():
    rules = prediction_schema()["properties"]["run"]["properties"]["rules"]
    assert rules["type"] == "object"
    assert rules.get("additionalProperties") is True
    assert "properties" not in rules
    assert "required" not in rules
    assert schema_path().is_file()


def test_rules_round_trip_keeps_caller_keys(tmp_path: Path):
    record = _example()
    record["run"]["rules"] = {
        "threshold": 0.05,
        "notes": ["keep", {"nested": True}],
        "unexpected": None,
    }
    store = _store(tmp_path)
    store.add([record])
    stored = store.read(datetime.strptime(DAY, "%Y-%m-%d").date())[0]
    assert stored["run"]["rules"] == record["run"]["rules"]


def test_add_rejects_bad_weights_timezone_and_wide_price():
    bad_weights = _example()
    bad_weights["run"]["weights"] = {**bad_weights["run"]["weights"], "rotation": 7}
    with pytest.raises(PredictionValidationError, match="weights must sum to 100"):
        validate_prediction(bad_weights, pending_only=True)
    missing = _example()
    del missing["run"]["weights"]["rotation"]
    with pytest.raises(PredictionValidationError, match="rotation is required"):
        validate_prediction(missing, pending_only=True)
    extra = _example()
    extra["run"]["weights"]["softmax_t"] = 1
    with pytest.raises(PredictionValidationError, match="softmax_t is not allowed"):
        validate_prediction(extra, pending_only=True)
    scored = _example()
    scored["horses"][0]["item_scores"]["ability"] = None
    with pytest.raises(PredictionValidationError, match="item_scores.ability must be number"):
        validate_prediction(scored, pending_only=True)

    naive = _example()
    naive["run"]["run_at"] = "2026-10-17T18:00:00"
    naive["record_id"] = "202610180811_day_before_20261017T180000"
    with pytest.raises(PredictionValidationError):
        validate_prediction(naive, pending_only=True)

    wide = _example()
    wide["bets"][1]["odds"] = 2.4
    with pytest.raises(PredictionValidationError, match="wide odds must be null"):
        validate_prediction(wide, pending_only=True)

    settled = _example()
    settled["result"] = {"status": "settled"}
    with pytest.raises(PredictionValidationError, match="result must be pending"):
        validate_prediction(settled, pending_only=True)


def test_duplicate_record_id_does_not_change_the_file(tmp_path: Path):
    store = _store(tmp_path)
    store.add([_example()])
    path = store.path_for(datetime.strptime(DAY, "%Y-%m-%d").date())
    before = path.read_bytes()
    changed = _example()
    changed["horses"][0]["total_score"] = 1
    with pytest.raises(DuplicateRecordError, match="duplicate record_id"):
        store.add([changed])
    assert path.read_bytes() == before
    assert store.read(datetime.strptime(DAY, "%Y-%m-%d").date())[0]["horses"][0]["total_score"] == 78.5


def test_editions_are_separate_lines_and_keep_order(tmp_path: Path):
    store = _store(tmp_path)
    later = _with_bets(
        [_bet("win", [7])],
        edition="pre_post_45m",
        run_at="2026-10-18T15:00:00+09:00",
    )
    store.add([_example(), later])
    rows = store.read(datetime.strptime(DAY, "%Y-%m-%d").date())
    assert [row["run"]["edition"] for row in rows] == ["day_before", "pre_post_45m"]
    assert rows[0]["record_id"] != rows[1]["record_id"]

    calls: list[str] = []

    def fetch(race_id: str) -> SettledRaceResult:
        calls.append(race_id)
        return _winner_card()

    _settle(store, fetch)
    assert calls == [RACE_ID]
    settled_rows = store.read(datetime.strptime(DAY, "%Y-%m-%d").date())
    assert [row["run"]["edition"] for row in settled_rows] == ["day_before", "pre_post_45m"]
    assert all(row["result"]["status"] == "settled" for row in settled_rows)


def test_win_wide_quinella_and_trio_hits_and_misses(tmp_path: Path):
    bets = [
        _bet("win", [7], stake=100),
        _bet("win", [3], stake=100),
        _bet("wide", [3, 7], stake=200),
        _bet("wide", [1, 3], stake=100),
        _bet("quinella", [3, 7], stake=100),
        _bet("quinella", [1, 7], stake=100),
        _bet("trio", [1, 3, 7], stake=100),
        _bet("trio", [1, 2, 7], stake=100),
    ]
    store = _store(tmp_path)
    store.add([_with_bets(bets)])
    fetched = _settled(
        results=[_finish(7, 1), _finish(3, 2), _finish(1, 3), _finish(2, 4)],
        payouts=[
            _payout("単勝", "7", 360),
            _payout("ワイド", "7-3", 180),
            _payout("馬連", "3-7", 540),
            _payout("三連複", "1-3-7", 1200),
        ],
    )
    _settle(store, lambda _race_id: fetched)
    result = store.read(datetime.strptime(DAY, "%Y-%m-%d").date())[0]["result"]
    lines = result["settlement"]["bets"]
    assert [(line["hit"], line["payout_yen"]) for line in lines] == [
        (True, 360),
        (False, 0),
        (True, 360),
        (False, 0),
        (True, 540),
        (False, 0),
        (True, 1200),
        (False, 0),
    ]
    assert result["settlement"]["stake_total_yen"] == 900
    assert result["settlement"]["payout_total_yen"] == 2460
    assert result["settlement"]["roi"] == 1.7333
    assert result["payouts"][1]["combination"] == [3, 7]


def test_reference_bet_is_outside_the_totals(tmp_path: Path):
    bets = [
        _bet("win", [7], stake=100),
        _bet("win", [7], status="reference", stake=1000),
    ]
    store = _store(tmp_path)
    store.add([_with_bets(bets)])
    _settle(store, lambda _race_id: _winner_card())
    settlement = store.read(datetime.strptime(DAY, "%Y-%m-%d").date())[0]["result"]["settlement"]
    assert settlement["bets"][1]["hit"] is True
    assert settlement["bets"][1]["payout_yen"] == 3600
    assert settlement["stake_total_yen"] == 100
    assert settlement["payout_total_yen"] == 360
    assert settlement["roi"] == 2.6


def test_scratched_horse_is_refunded_at_stake(tmp_path: Path):
    bets = [
        _bet("win", [4], stake=100),
        _bet("wide", [4, 7], stake=300),
        _bet("win", [7], stake=100),
    ]
    store = _store(tmp_path)
    store.add([_with_bets(bets)])
    fetched = _settled(
        results=[_finish(7, 1), _finish(4, None, "scratched")],
        payouts=[_payout("単勝", "7", 200)],
        refunds=None,
    )
    _settle(store, lambda _race_id: fetched)
    lines = store.read(datetime.strptime(DAY, "%Y-%m-%d").date())[0]["result"]["settlement"]
    assert lines["bets"][0] == {"index": 0, "hit": False, "refunded": True, "payout_yen": 100}
    assert lines["bets"][1]["refunded"] is True
    assert lines["bets"][1]["payout_yen"] == 300
    assert lines["bets"][2]["hit"] is True
    assert lines["stake_total_yen"] == 500
    assert lines["payout_total_yen"] == 600
    assert lines["roi"] == 0.2


def test_refund_list_does_not_spread_to_other_tickets(tmp_path: Path):
    bets = [
        _bet("win", [5], stake=100),
        _bet("wide", [1, 5], stake=100),
    ]
    store = _store(tmp_path)
    store.add([_with_bets(bets)])
    fetched = _settled(
        results=[_finish(1, 1), _finish(5, 8)],
        payouts=[_payout("ワイド", "1-2", 150)],
        refunds=[
            RaceRefund(
                horse_numbers=[5],
                bet_types=["単勝"],
                source="netkeiba_html",
                fetched_at=FETCHED,
            )
        ],
    )
    _settle(store, lambda _race_id: fetched)
    result = store.read(datetime.strptime(DAY, "%Y-%m-%d").date())[0]["result"]
    assert result["settlement"]["bets"][0]["refunded"] is True
    assert result["settlement"]["bets"][0]["payout_yen"] == 100
    assert result["settlement"]["bets"][1] == {
        "index": 1,
        "hit": False,
        "refunded": False,
        "payout_yen": 0,
    }
    assert "refund horses are not scratched or excluded: 5" in result["warning"]


def test_dead_heat_uses_each_payout_row(tmp_path: Path):
    bets = [
        _bet("win", [1], stake=100),
        _bet("win", [2], stake=100),
        _bet("win", [3], stake=100),
    ]
    store = _store(tmp_path)
    store.add([_with_bets(bets)])
    fetched = _settled(
        results=[_finish(1, 1), _finish(2, 1), _finish(3, 3)],
        payouts=[_payout("単勝", "1", 150), _payout("単勝", "2", 220)],
    )
    _settle(store, lambda _race_id: fetched)
    result = store.read(datetime.strptime(DAY, "%Y-%m-%d").date())[0]["result"]
    assert [row["dead_heat"] for row in result["finishes"]] == [True, True, False]
    assert [(row["combination"], row["yen_per_100"]) for row in result["payouts"]] == [
        ([1], 150),
        ([2], 220),
    ]
    assert [line["payout_yen"] for line in result["settlement"]["bets"]] == [150, 220, 0]
    assert 185 not in [line["payout_yen"] for line in result["settlement"]["bets"]]


def test_disagreeing_payout_rows_stay_null(tmp_path: Path):
    store = _store(tmp_path)
    store.add([_with_bets([_bet("win", [1], stake=100)])])
    fetched = _settled(
        results=[_finish(1, 1)],
        payouts=[_payout("単勝", "1", 150), _payout("単勝", "1", 180)],
    )
    _settle(store, lambda _race_id: fetched)
    result = store.read(datetime.strptime(DAY, "%Y-%m-%d").date())[0]["result"]
    assert result["settlement"]["bets"][0]["hit"] is True
    assert result["settlement"]["bets"][0]["payout_yen"] is None
    assert result["settlement"]["payout_total_yen"] is None
    assert result["settlement"]["roi"] is None
    assert "payout rows disagree for win 1" in result["warning"]


def test_non_integer_payout_stays_null(tmp_path: Path):
    store = _store(tmp_path)
    store.add([_with_bets([_bet("win", [1], stake=50)])])
    fetched = _settled(results=[_finish(1, 1)], payouts=[_payout("単勝", "1", 333)])
    _settle(store, lambda _race_id: fetched)
    result = store.read(datetime.strptime(DAY, "%Y-%m-%d").date())[0]["result"]
    assert result["settlement"]["bets"][0]["payout_yen"] is None
    assert result["settlement"]["roi"] is None
    assert "payout is not an integer yen" in result["warning"]


@pytest.mark.parametrize(
    ("phase", "complete"),
    [
        ("provisional", False),
        ("provisional", True),
        ("confirmed", False),
        (None, True),
    ],
)
def test_race_that_is_not_final_is_left_untouched(tmp_path: Path, phase: str | None, complete: bool):
    store = _store(tmp_path)
    store.add([_example()])
    path = store.path_for(datetime.strptime(DAY, "%Y-%m-%d").date())
    before = path.read_bytes()
    fetched = _settled(phase=phase, complete=complete, results=[_finish(7, 1)])
    report = _settle(store, lambda _race_id: fetched)
    assert path.read_bytes() == before
    assert report["settled"] == []
    assert report["pending"][0]["record_id"] == _example()["record_id"]
    assert report["pending"][0]["result_phase"] == phase
    assert report["pending"][0]["complete"] is complete
    assert store.read(datetime.strptime(DAY, "%Y-%m-%d").date())[0]["result"] == {"status": "pending"}


def test_forbidden_fetch_is_not_retried(tmp_path: Path):
    store = _store(tmp_path)
    store.add([_example()])
    path = store.path_for(datetime.strptime(DAY, "%Y-%m-%d").date())
    before = path.read_bytes()
    calls: list[str] = []

    def fetch(race_id: str) -> SettledRaceResult:
        calls.append(race_id)
        return _settled(
            phase=None,
            complete=False,
            http_status=403,
            warning="race result returned 403",
        )

    report = _settle(store, fetch)
    assert calls == [RACE_ID]
    assert path.read_bytes() == before
    assert report["pending"][0]["warning"] == "race result returned 403"


def test_settle_keeps_prediction_fields_and_a_second_pass_does_not_refetch(tmp_path: Path):
    store = _store(tmp_path)
    original = store.add([_example()])[0]
    before = deepcopy(original)
    calls: list[str] = []

    def fetch(race_id: str) -> SettledRaceResult:
        calls.append(race_id)
        card = _winner_card()
        card.race_name = "別の名前"
        return card

    _settle(store, fetch)
    path = store.path_for(datetime.strptime(DAY, "%Y-%m-%d").date())
    written = path.read_bytes()
    stored = store.read(datetime.strptime(DAY, "%Y-%m-%d").date())[0]
    result = stored.pop("result")
    before.pop("result")
    assert stored == before
    assert stored["race"]["race_name"] == "例"
    assert result["status"] == "settled"
    assert result["filled_at"] == "2026-10-18T16:45:00+09:00"
    assert result["result_phase"] == "confirmed"
    assert result["complete"] is True

    _settle(store, fetch)
    assert calls == [RACE_ID]
    assert path.read_bytes() == written
    assert list(tmp_path.glob("*.tmp")) == []


def test_missing_ticket_payout_stays_null(tmp_path: Path):
    store = _store(tmp_path)
    store.add([_with_bets([_bet("quinella", [1, 2], stake=100)])])
    fetched = _settled(results=[_finish(1, 1), _finish(2, 2)], payouts=[_payout("単勝", "1", 200)])
    _settle(store, lambda _race_id: fetched)
    result = store.read(datetime.strptime(DAY, "%Y-%m-%d").date())[0]["result"]
    assert result["settlement"]["bets"][0]["hit"] is None
    assert result["settlement"]["bets"][0]["payout_yen"] is None
    assert "quinella payout was not on the result" in result["warning"]


def test_cli_add_show_and_settle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(
        RaceService,
        "get_settled_race_result",
        lambda self, race_id, **kwargs: _winner_card(),
    )

    def run(*argv: str) -> str:
        monkeypatch.setattr("sys.argv", ["cli", *argv])
        cli_main()
        captured = capsys.readouterr()
        return captured.out

    added = json.loads(run("predictions", "add", str(FIXTURE), "--dir", str(tmp_path), "--json"))
    assert added[0]["record_id"] == _example()["record_id"]
    shown = json.loads(run("predictions", "show", "--date", "20261018", "--dir", str(tmp_path), "--json"))
    assert shown["records"][0]["_example"] is True
    report = json.loads(
        run("predictions", "settle", "--date", "2026-10-18", "--dir", str(tmp_path), "--json")
    )
    assert report["settled"][0]["race_id"] == RACE_ID
    assert report["pending"] == []


def test_api_add_show_settle_and_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HRP_PREDICTIONS_DIR", str(tmp_path))
    body = _example()
    with TestClient(app) as client:
        import backend.main as main

        def fetch(race_id: str, **kwargs: object) -> SettledRaceResult:
            assert kwargs.get("wait_complete", False) is False
            return _winner_card()

        assert main.race_service is not None
        monkeypatch.setattr(main.race_service, "get_settled_race_result", fetch)
        created = client.post("/api/predictions", json=body)
        assert created.status_code == 200
        assert created.json()["records"][0]["record_id"] == body["record_id"]
        duplicate = client.post("/api/predictions", json=body)
        assert duplicate.status_code == 409
        shown = client.get("/api/predictions", params={"date": "20261018"})
        assert shown.status_code == 200
        assert shown.json()["records"][0]["result"]["status"] == "pending"
        bad = client.get("/api/predictions", params={"date": "not-a-date"})
        assert bad.status_code == 400
        report = client.post("/api/predictions/settle", params={"date": "20261018"})
        assert report.status_code == 200
        assert report.json()["settled"][0]["record_id"] == body["record_id"]
        assert client.get("/api/predictions", params={"date": "20261018"}).json()["records"][0]["result"][
            "status"
        ] == "settled"


def test_reference_without_stake_does_not_warn(tmp_path: Path):
    bets = [
        _bet("win", [7], stake=100),
        _bet("win", [7], status="reference", stake=None),
        _bet("win", [4], status="reference", stake=None),
    ]
    store = _store(tmp_path)
    store.add([_with_bets(bets)])
    fetched = _settled(
        results=[_finish(7, 1), _finish(4, None, "scratched")],
        payouts=[_payout("単勝", "7", 360)],
    )
    _settle(store, lambda _race_id: fetched)
    result = store.read(datetime.strptime(DAY, "%Y-%m-%d").date())[0]["result"]
    lines = result["settlement"]["bets"]
    assert lines[1]["hit"] is True
    assert lines[1]["payout_yen"] is None
    assert lines[2]["refunded"] is True
    assert lines[2]["payout_yen"] is None
    assert result["warning"] is None
    assert result["settlement"]["stake_total_yen"] == 100
    assert result["settlement"]["payout_total_yen"] == 360
    assert result["settlement"]["roi"] == 2.6


def test_concurrent_adds_of_distinct_records_all_persist(tmp_path: Path):
    import multiprocessing

    records = []
    for number in range(1, 13):
        record = _with_bets([_bet("win", [7])], race_id=f"2026101808{number:02d}")
        record["race"]["race_number"] = number
        records.append(record)
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(len(records)) as pool:
        jobs = [pool.apply_async(_mp_add, (str(tmp_path), record)) for record in records]
        results = [job.get(timeout=60) for job in jobs]
    assert results == ["ok"] * 12
    stored = PredictionStore(tmp_path).read(datetime.strptime(DAY, "%Y-%m-%d").date())
    assert len(stored) == 12
    assert {row["record_id"] for row in stored} == {row["record_id"] for row in records}


def test_concurrent_adds_of_the_same_record_id_keep_one(tmp_path: Path):
    import multiprocessing

    record = _example()
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(6) as pool:
        jobs = [pool.apply_async(_mp_add, (str(tmp_path), record)) for _ in range(6)]
        results = [job.get(timeout=60) for job in jobs]
    assert sorted(results) == ["duplicate"] * 5 + ["ok"]
    stored = PredictionStore(tmp_path).read(datetime.strptime(DAY, "%Y-%m-%d").date())
    assert [row["record_id"] for row in stored] == [record["record_id"]]


def test_add_during_settle_keeps_the_added_record(tmp_path: Path):
    import multiprocessing

    store = _store(tmp_path)
    original = store.add([_example()])[0]
    started = tmp_path / "fetch-started"
    release = tmp_path / "fetch-release"
    report_path = tmp_path / "settle-report.json"
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(
        target=_mp_settle,
        args=(str(tmp_path), str(started), str(release), str(report_path)),
    )
    proc.start()
    try:
        deadline = time.time() + 20
        while not started.exists():
            if proc.exitcode is not None:
                raise AssertionError(f"settle exited early: {proc.exitcode}")
            if time.time() > deadline:
                raise AssertionError("settle did not reach the fetch")
            time.sleep(0.02)
        extra = _with_bets([_bet("win", [1])], race_id="202610180812")
        extra["race"]["race_number"] = 12
        store.add([extra])
        release.write_text("go", encoding="utf-8")
        proc.join(30)
    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join(5)
    assert proc.exitcode == 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["fetched"] == [RACE_ID]
    rows = store.read(datetime.strptime(DAY, "%Y-%m-%d").date())
    by_id = {row["record_id"]: row for row in rows}
    assert set(by_id) == {original["record_id"], extra["record_id"]}
    assert by_id[original["record_id"]]["result"]["status"] == "settled"
    assert by_id[extra["record_id"]]["result"] == {"status": "pending"}


def test_windows_lock_uses_msvcrt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import backend.services.prediction_store as store_mod

    calls: list[int] = []

    class _Msvcrt:
        LK_LOCK = 0
        LK_UNLCK = 1

        def locking(self, fd: int, mode: int, length: int) -> None:
            assert length == 1
            calls.append(mode)

    monkeypatch.setattr(store_mod, "fcntl", None)
    monkeypatch.setattr(store_mod, "msvcrt", _Msvcrt())
    PredictionStore(tmp_path).add([_example()])
    assert calls[0] == _Msvcrt.LK_LOCK
    assert _Msvcrt.LK_UNLCK in calls
    assert PredictionStore(tmp_path).read(datetime.strptime(DAY, "%Y-%m-%d").date())


def test_missing_file_lock_is_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import backend.services.prediction_store as store_mod

    monkeypatch.setattr(store_mod, "fcntl", None)
    monkeypatch.setattr(store_mod, "msvcrt", None)
    with pytest.raises(PredictionStoreError, match="fcntl.flock or msvcrt.locking"):
        PredictionStore(tmp_path).add([_example()])
    assert list(tmp_path.glob("*.jsonl")) == []


def test_schema_path_uses_package_data_and_repo_fallback(monkeypatch: pytest.MonkeyPatch):
    import backend.services.prediction_validate as validate

    path = schema_path()
    assert path.is_file()
    assert path.name == "hrp.prediction.schema.json"
    weights = prediction_schema()["properties"]["run"]["properties"]["weights"]
    scores = prediction_schema()["properties"]["horses"]["items"]["properties"]["item_scores"]
    keys = [
        "ability",
        "pace",
        "course",
        "going",
        "workout",
        "gate_jockey",
        "pedigree",
        "rotation",
    ]
    assert weights["type"] == "object"
    assert weights["additionalProperties"] is False
    assert weights["required"] == keys
    assert scores["required"] == keys
    assert "default" not in weights
    docs = Path(__file__).resolve().parents[2] / "docs" / "schemas" / "hrp.prediction.schema.json"
    packaged = Path(__file__).resolve().parents[1] / "src" / "services" / "hrp.prediction.schema.json"
    assert packaged.read_bytes() == docs.read_bytes()
    monkeypatch.setattr(validate, "_packaged_schema_path", lambda: None)
    assert validate.schema_path() == docs


def test_contract_names_the_weight_keys():
    text = (Path(__file__).resolve().parents[2] / "docs" / "ev-contract.md").read_text(encoding="utf-8")
    section = text.split("`weights` と `item_scores` は、同じ8つのキー", 1)[1].split("###", 1)[0]
    keys = [
        "ability",
        "pace",
        "course",
        "going",
        "workout",
        "gate_jockey",
        "pedigree",
        "rotation",
    ]
    meanings = [
        "基礎能力・近走のレース内容",
        "展開・脚質適性",
        "コース・距離適性",
        "馬場適性・当日バイアス",
        "調教・仕上がり・馬体",
        "枠順・斤量・騎手",
        "血統適性",
        "ローテーション・状態リスク",
    ]
    assert [section.index(key) for key in keys] == sorted(section.index(key) for key in keys)
    for meaning in meanings:
        assert meaning in section
    assert "softmax_t" in section
    assert "近走, 血統, 脚質" not in text
