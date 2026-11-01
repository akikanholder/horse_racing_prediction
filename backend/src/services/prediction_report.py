"""Read-only reports over prediction JSONL.

Export, calibration, edition diff, and ROI use the records as written.
A missing price, finish, or settlement stays null and is named in ``warning``.
Nothing here computes a probability, an odds price, or a stake.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, TypeGuard

from backend.services.prediction_store import PredictionStore
from backend.services.prediction_validate import PredictionValidationError

_RACE_ID = re.compile(r"^[0-9]{12}$")
_QUANT = Decimal("0.0001")
_DELTA = Decimal("0.000001")
_BY_KEYS = ("day", "bet_type", "surface", "odds_band")
_DEFAULT_BANDS = "1,3,5,10,20,50,100"
_BET_FIELDS = (
    "prob",
    "odds",
    "odds_min",
    "odds_max",
    "ev",
    "ev_conservative",
    "stake_yen",
    "status",
    "reason",
)
EXPORT_FIELDS = (
    "record_id",
    "race_id",
    "edition",
    "run_at",
    "date",
    "venue",
    "surface",
    "distance_m",
    "going",
    "field_size",
    "horse_number",
    "horse_name",
    "win_prob",
    "odds_at_prediction",
    "final_odds",
    "finish_position",
    "won",
    "warning",
)
ROI_METRICS = (
    "bet_count",
    "hit_count",
    "refund_count",
    "stake_yen",
    "return_yen",
    "refund_yen",
    "profit_yen",
    "hit_rate",
    "roi",
    "warning",
)
FINAL_ODDS_NOT_ON_RECORD = "final win odds were not on the prediction record"
NO_SCORED = "no settled win probabilities to score"
LOG_LOSS_UNDEFINED = "log loss is undefined when win_prob is 0 or 1"
WIDE_ODDS_RANGE = "wide odds are a range and were not banded"


@dataclass
class FinalWinOdds:
    """Printed win odds from a confirmed complete result.

    ``by_race[race_id][horse_number]`` is the printed price, or None when that
    cell was blank. ``race_warning`` names a race that was not final, or a
    fetch that stopped. This object does not turn a payout into a price.
    """

    by_race: dict[str, dict[int, float | None]] = field(default_factory=dict)
    race_warning: dict[str, str] = field(default_factory=dict)


def parse_bins(value: int) -> int:
    """Equal-width probability bins. 1 through 100."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise PredictionValidationError("probability bins must be from 1 to 100")
    return value


def parse_by(value: str | None) -> list[str]:
    """Comma-separated grouping. Unknown names are rejected."""
    text = value if value else ",".join(_BY_KEYS)
    keys: list[str] = []
    for part in text.split(","):
        name = part.strip()
        if not name:
            continue
        if name not in _BY_KEYS:
            raise PredictionValidationError(f"unknown roi grouping: {name}")
        if name not in keys:
            keys.append(name)
    if not keys:
        raise PredictionValidationError("roi grouping is empty")
    return keys


def parse_odds_bands(value: str | None) -> list[tuple[Decimal, str]]:
    """Increasing edges. The default is 1, 3, 5, 10, 20, 50, 100."""
    text = value if value else _DEFAULT_BANDS
    edges: list[tuple[Decimal, str]] = []
    for part in text.split(","):
        label = part.strip()
        if not label:
            raise PredictionValidationError("odds bands must be increasing numbers")
        try:
            number = Decimal(label)
        except Exception as exc:
            raise PredictionValidationError("odds bands must be increasing numbers") from exc
        if not number.is_finite() or number <= 0:
            raise PredictionValidationError("odds bands must be increasing numbers")
        edges.append((number, label))
    if len(edges) < 1 or any(
        edges[index][0] >= edges[index + 1][0] for index in range(len(edges) - 1)
    ):
        raise PredictionValidationError("odds bands must be increasing numbers")
    return edges


def parse_race_id(value: str | None) -> str | None:
    """Optional 12-digit race id."""
    if value is None or value == "":
        return None
    if _RACE_ID.fullmatch(value) is None:
        raise PredictionValidationError("race must be a 12-digit race_id")
    return value


def iter_meeting_records(store: PredictionStore, start: date, end: date) -> list[dict[str, Any]]:
    """Records from ``start`` through ``end``, file order within each day."""
    if end < start:
        raise PredictionValidationError("date range ends before it starts")
    records: list[dict[str, Any]] = []
    day = start
    while day <= end:
        records.extend(store.read(day))
        day += timedelta(days=1)
    return records


def export_backtest(
    store: PredictionStore,
    start: date,
    end: date,
    *,
    bins: int = 10,
    final_win_odds: FinalWinOdds | None = None,
) -> dict[str, Any]:
    """One row per horse per prediction run, plus a calibration table.

    Finish and the win flag come from a settled ``result``. Final win odds
    come only from ``final_win_odds``. A payout is not turned into a price.
    """
    count = parse_bins(bins)
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for record in iter_meeting_records(store, start, end):
        for row in _export_rows(record, final_win_odds):
            rows.append(row)
            if row["warning"]:
                warnings.append(row["warning"])
    calibration = _calibration(rows, count)
    if calibration["warning"]:
        warnings.append(calibration["warning"])
    if not rows:
        warnings.append("no prediction records")
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "rows": rows,
        "calibration": calibration,
        "warning": _join(*warnings),
    }


def diff_editions(
    store: PredictionStore,
    day: date,
    *,
    race_id: str | None = None,
) -> dict[str, Any]:
    """Compare day_before and pre_post_45m for each race on one meeting day.

    Probabilities, marks, and bets are the stored fields. Profit uses the
    settled stake and payout totals. An edition that is still pending has a
    null profit and a warning.
    """
    selected = parse_race_id(race_id)
    records = store.read(day)
    if selected is not None:
        records = [record for record in records if _race_id(record) == selected]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        identity = _race_id(record)
        if identity is None:
            continue
        grouped[identity].append(record)
    pairs: list[dict[str, Any]] = []
    unpaired: list[dict[str, Any]] = []
    warnings: list[str] = []
    for identity in grouped:
        before = _edition_rows(grouped[identity], "day_before")
        same = _edition_rows(grouped[identity], "pre_post_45m")
        if not before or not same:
            missing = "pre_post_45m" if before else "day_before"
            for record in before or same:
                note = f"no {missing} record for this race"
                unpaired.append(_unpaired(record, note))
                warnings.append(note)
            continue
        extra: list[str] = []
        if len(before) > 1:
            extra.append("more than one day_before record")
        if len(same) > 1:
            extra.append("more than one pre_post_45m record")
        for left in before:
            for right in same:
                pair = _diff_pair(left, right, extra)
                pairs.append(pair)
                if pair["warning"]:
                    warnings.append(pair["warning"])
    if not records:
        note = (
            "no prediction records" if selected is None else "no prediction records for this race"
        )
        warnings.append(note)
    return {
        "date": day.isoformat(),
        "race_id": selected,
        "pairs": pairs,
        "unpaired": unpaired,
        "warning": _join(*warnings),
    }


def roi_summary(
    store: PredictionStore,
    start: date,
    end: date,
    *,
    by: str | None = None,
    odds_bands: str | None = None,
) -> dict[str, Any]:
    """ROI and hit rate for bought bets. Reference bets stay out.

    Refunded tickets stay in the stake and the return (the stake comes back)
    and stay out of the hit rate. A wide ticket has no single odds, so its
    odds band is null.
    """
    keys = parse_by(by)
    edges = parse_odds_bands(odds_bands)
    warnings: list[str] = []
    lines: list[_RoiLine] = []
    for record in iter_meeting_records(store, start, end):
        parsed, notes = _roi_lines(record, edges)
        lines.extend(parsed)
        warnings.extend(notes)
    grouped: dict[tuple[Any, ...], list[_RoiLine]] = defaultdict(list)
    for line in lines:
        grouped[_group_key(line, keys)].append(line)
    groups: list[dict[str, Any]] = []
    for key in sorted(
        grouped,
        key=lambda item: tuple(
            (value is None, "" if value is None else str(value)) for value in item
        ),
    ):
        group = _roi_group(keys, key, grouped[key])
        groups.append(group)
        if group["warning"]:
            warnings.append(group["warning"])
    if not lines and not warnings:
        warnings.append("no bought bets")
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "by": keys,
        "odds_bands": [label for _number, label in edges],
        "groups": groups,
        "warning": _join(*warnings),
    }


def rows_as_csv(rows: list[dict[str, Any]]) -> str:
    """Horse rows. An empty cell is null."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(EXPORT_FIELDS), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_cell(row.get(key)) for key in EXPORT_FIELDS})
    return buffer.getvalue()


def rows_as_jsonl(rows: list[dict[str, Any]]) -> str:
    """One horse object per line. Nulls stay null."""
    return "".join(
        json.dumps({key: row.get(key) for key in EXPORT_FIELDS}, ensure_ascii=False) + "\n"
        for row in rows
    )


def roi_as_csv(report: Mapping[str, Any]) -> str:
    """One group per line. An empty cell is null."""
    keys = list(report["by"])
    fields = keys + list(ROI_METRICS)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for group in report["groups"]:
        writer.writerow({key: _csv_cell(group.get(key)) for key in fields})
    return buffer.getvalue()


def format_calibration(report: Mapping[str, Any]) -> str:
    """Text calibration table, Brier score, and log loss."""
    table = report["calibration"]
    lines = [
        f"範囲: {report['from']} から {report['to']}",
        f"行数: {len(report['rows'])}",
        "確率帯 件数 平均予測 実勝率",
    ]
    for bucket in table["bins"]:
        lines.append(
            f"{_plain(bucket['lo'])}-{_plain(bucket['hi'])} "
            f"{bucket['count']} {_plain(bucket['mean_predicted'])} {_plain(bucket['observed_win_rate'])}"
        )
    lines.append(f"Brier: {_plain(table['brier'])}")
    lines.append(f"log loss: {_plain(table['log_loss'])}")
    lines.append(f"採点: {table['scored']} 対象外: {table['skipped']}")
    if report.get("warning"):
        lines.append(f"警告: {report['warning']}")
    return "\n".join(lines) + "\n"


def format_diff(report: Mapping[str, Any]) -> str:
    """Text edition diff."""
    lines = [f"開催日: {report['date']}"]
    if report.get("race_id"):
        lines.append(f"レース: {report['race_id']}")
    if not report["pairs"] and not report["unpaired"]:
        lines.append("なし")
    for pair in report["pairs"]:
        lines.append(
            f"\n--- {pair['race_id']} {pair['day_before']['record_id']} -> {pair['pre_post_45m']['record_id']} ---"
        )
        for horse in pair["horses"]:
            lines.append(
                f"馬番{horse['horse_number']} "
                f"確率 {_plain(horse['win_prob_day_before'])} -> {_plain(horse['win_prob_pre_post_45m'])} "
                f"差 {_plain(horse['win_prob_delta'])} "
                f"印 {horse['buy_mark_day_before'] or '-'} -> {horse['buy_mark_pre_post_45m'] or '-'}"
            )
        bets = pair["bets"]
        lines.append(
            f"馬券 追加 {len(bets['added'])} 削除 {len(bets['removed'])} 変更 {len(bets['changed'])}"
        )
        profit = pair["profit"]
        lines.append(
            f"収支 {_plain(profit['day_before_profit_yen'])} -> {_plain(profit['pre_post_45m_profit_yen'])} "
            f"差 {_plain(profit['profit_delta_yen'])}"
        )
        if pair["warning"]:
            lines.append(f"警告: {pair['warning']}")
    for row in report["unpaired"]:
        lines.append(f"対なし {row['record_id']} {row['edition']} {row['warning']}")
    if report.get("warning"):
        lines.append(f"警告: {report['warning']}")
    return "\n".join(lines) + "\n"


def format_roi(report: Mapping[str, Any]) -> str:
    """Text ROI table."""
    keys = list(report["by"])
    header = " ".join(keys + ["件数", "的中", "返還", "賭け金", "払戻", "収支", "的中率", "ROI"])
    lines = [f"範囲: {report['from']} から {report['to']}", header]
    if not report["groups"]:
        lines.append("なし")
    for group in report["groups"]:
        labels = " ".join(_plain(group.get(key)) for key in keys)
        lines.append(
            f"{labels} {group['bet_count']} {group['hit_count']} {group['refund_count']} "
            f"{_plain(group['stake_yen'])} {_plain(group['return_yen'])} {_plain(group['profit_yen'])} "
            f"{_plain(group['hit_rate'])} {_plain(group['roi'])}"
        )
        if group["warning"]:
            lines.append(f"警告: {group['warning']}")
    if report.get("warning"):
        lines.append(f"警告: {report['warning']}")
    return "\n".join(lines) + "\n"


def _export_rows(record: dict[str, Any], odds: FinalWinOdds | None) -> list[dict[str, Any]]:
    race = _as_dict(record.get("race"))
    run = _as_dict(record.get("run"))
    horses = _as_list(record.get("horses"))
    rows: list[dict[str, Any]] = []
    base_notes: list[str] = []
    race_id = race.get("race_id") if isinstance(race.get("race_id"), str) else None
    if race_id is None:
        base_notes.append("race_id was not on the record")
    for horse in horses:
        if not isinstance(horse, dict):
            continue
        notes = list(base_notes)
        number = horse.get("horse_number")
        if not isinstance(number, int):
            notes.append("horse number was not on the record")
            number = None
        finish, finish_note = _finish(record, number)
        if finish_note:
            notes.append(finish_note)
        final_odds, odds_note = _final_odds(odds, race_id, number)
        if odds_note:
            notes.append(odds_note)
        win_prob = horse.get("win_prob")
        if win_prob is None:
            notes.append("win_prob was not on the record")
        elif not _is_number(win_prob):
            win_prob = None
            notes.append("win_prob was not on the record")
        price = horse.get("odds_win")
        if price is None:
            notes.append("odds at prediction time were not on the record")
        elif not _is_number(price):
            price = None
            notes.append("odds at prediction time were not on the record")
        rows.append(
            {
                "record_id": record.get("record_id"),
                "race_id": race_id,
                "edition": run.get("edition"),
                "run_at": run.get("run_at"),
                "date": race.get("date"),
                "venue": race.get("venue"),
                "surface": race.get("surface"),
                "distance_m": race.get("distance_m"),
                "going": race.get("going"),
                "field_size": race.get("field_size"),
                "horse_number": number,
                "horse_name": horse.get("horse_name"),
                "win_prob": win_prob,
                "odds_at_prediction": price,
                "final_odds": final_odds,
                "finish_position": finish["finish_position"],
                "won": finish["won"],
                "warning": _join(*notes),
            }
        )
    return rows


def _finish(record: dict[str, Any], horse_number: int | None) -> tuple[dict[str, Any], str | None]:
    empty = {"finish_position": None, "won": None}
    result = record.get("result")
    if not isinstance(result, dict) or result.get("status") != "settled":
        return empty, "result was not settled"
    if horse_number is None:
        return empty, "finish was not on the result"
    finishes = result.get("finishes")
    if not isinstance(finishes, list):
        return empty, f"finish was not on the result: {horse_number}"
    matches = [
        row for row in finishes if isinstance(row, dict) and row.get("horse_number") == horse_number
    ]
    if len(matches) != 1:
        return empty, f"finish was not on the result: {horse_number}"
    rank = matches[0].get("rank")
    status = matches[0].get("status")
    if not isinstance(rank, int) or isinstance(rank, bool):
        return empty, f"finish position was not on the result: {horse_number}"
    if status != "finished":
        return empty, f"finish position was not on the result: {horse_number}"
    return {"finish_position": rank, "won": rank == 1}, None


def _final_odds(
    odds: FinalWinOdds | None,
    race_id: str | None,
    horse_number: int | None,
) -> tuple[float | None, str | None]:
    if odds is None:
        return None, FINAL_ODDS_NOT_ON_RECORD
    if race_id is None:
        return None, FINAL_ODDS_NOT_ON_RECORD
    if race_id in odds.race_warning:
        return None, odds.race_warning[race_id]
    if horse_number is None:
        return None, "final win odds were not on the result"
    race = odds.by_race.get(race_id)
    if race is None or horse_number not in race:
        return None, f"final win odds were not on the result: {horse_number}"
    value = race[horse_number]
    if not _is_number(value) or not math.isfinite(float(value)):
        return None, f"final win odds were not on the result: {horse_number}"
    return float(value), None


def _calibration(rows: list[dict[str, Any]], bins: int) -> dict[str, Any]:
    scored: list[tuple[Decimal, bool]] = []
    skipped = 0
    notes: list[str] = []
    for row in rows:
        prob = row.get("win_prob")
        won = row.get("won")
        if not _is_number(prob) or not isinstance(won, bool):
            skipped += 1
            continue
        number = Decimal(str(prob))
        if number < 0 or number > 1:
            skipped += 1
            notes.append("win_prob was outside 0 to 1")
            continue
        scored.append((number, won))
    width = Decimal(1) / Decimal(bins)
    buckets: list[list[tuple[Decimal, bool]]] = [[] for _ in range(bins)]
    for prob, won in scored:
        if prob <= 0:
            index = 0
        elif prob >= 1:
            index = bins - 1
        else:
            index = min(bins - 1, int(prob / width))
        buckets[index].append((prob, won))
    table: list[dict[str, Any]] = []
    brier_sum = Decimal(0)
    loss_sum = Decimal(0)
    loss_defined = True
    for index, bucket in enumerate(buckets):
        lo = Decimal(0) if index == 0 else width * index
        hi = Decimal(1) if index == bins - 1 else width * (index + 1)
        if not bucket:
            table.append(
                {
                    "lo": _q(lo),
                    "hi": _q(hi),
                    "count": 0,
                    "mean_predicted": None,
                    "observed_win_rate": None,
                }
            )
            continue
        total = sum((prob for prob, _won in bucket), Decimal(0))
        wins = sum(1 for _prob, won in bucket if won)
        table.append(
            {
                "lo": _q(lo),
                "hi": _q(hi),
                "count": len(bucket),
                "mean_predicted": _q4(total / Decimal(len(bucket))),
                "observed_win_rate": _q4(Decimal(wins) / Decimal(len(bucket))),
            }
        )
        for prob, won in bucket:
            target = Decimal(1) if won else Decimal(0)
            brier_sum += (prob - target) ** 2
            term = _log_loss_term(prob, won)
            if term is None:
                loss_defined = False
            else:
                loss_sum += term
    if skipped:
        notes.append("calibration skipped rows without a settled win flag or win_prob")
    if not scored:
        notes.append(NO_SCORED)
    elif not loss_defined:
        notes.append(LOG_LOSS_UNDEFINED)
    return {
        "bin_count": bins,
        "bins": table,
        "scored": len(scored),
        "skipped": skipped,
        "brier": None if not scored else _q4(brier_sum / Decimal(len(scored))),
        "log_loss": None
        if not scored or not loss_defined
        else _q4(loss_sum / Decimal(len(scored))),
        "warning": _join(*notes),
    }


def _log_loss_term(prob: Decimal, won: bool) -> Decimal | None:
    """Natural-log loss. A probability of 0 or 1 is defined only when it matches."""
    if prob == 0:
        return Decimal(0) if not won else None
    if prob == 1:
        return Decimal(0) if won else None
    probability = float(prob)
    if won:
        return Decimal(str(-math.log(probability)))
    return Decimal(str(-math.log(1 - probability)))


def _diff_pair(
    left: dict[str, Any],
    right: dict[str, Any],
    extra: list[str],
) -> dict[str, Any]:
    notes = list(extra)
    horses, horse_notes = _diff_horses(left, right)
    notes.extend(horse_notes)
    bets, bet_notes = _diff_bets(left, right)
    notes.extend(bet_notes)
    profit, profit_note = _diff_profit(left, right)
    if profit_note:
        notes.append(profit_note)
    return {
        "race_id": _race_id(left),
        "day_before": _run_ref(left),
        "pre_post_45m": _run_ref(right),
        "horses": horses,
        "bets": bets,
        "profit": profit,
        "warning": _join(*notes),
    }


def _diff_horses(
    left: dict[str, Any],
    right: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    before = _horses_by_number(left)
    same = _horses_by_number(right)
    notes: list[str] = []
    rows: list[dict[str, Any]] = []
    for number in sorted(set(before) | set(same)):
        horse_notes: list[str] = []
        former = before.get(number)
        latter = same.get(number)
        if former is None:
            horse_notes.append(f"horse was not on the day_before record: {number}")
        if latter is None:
            horse_notes.append(f"horse was not on the pre_post_45m record: {number}")
        left_prob = None if former is None else former.get("win_prob")
        right_prob = None if latter is None else latter.get("win_prob")
        if former is not None and left_prob is None:
            horse_notes.append(f"win_prob was not on the record: {number}")
        if latter is not None and right_prob is None:
            horse_notes.append(f"win_prob was not on the record: {number}")
        present = former is not None and latter is not None
        marks_changed = None
        if present:
            marks_changed = former.get("ability_mark") != latter.get("ability_mark") or former.get(
                "buy_mark"
            ) != latter.get("buy_mark")
        notes.extend(horse_notes)
        rows.append(
            {
                "horse_number": number,
                "win_prob_day_before": left_prob if _is_number(left_prob) else None,
                "win_prob_pre_post_45m": right_prob if _is_number(right_prob) else None,
                "win_prob_delta": _delta(left_prob, right_prob) if present else None,
                "ability_mark_day_before": None if former is None else former.get("ability_mark"),
                "ability_mark_pre_post_45m": None if latter is None else latter.get("ability_mark"),
                "buy_mark_day_before": None if former is None else former.get("buy_mark"),
                "buy_mark_pre_post_45m": None if latter is None else latter.get("buy_mark"),
                "marks_changed": marks_changed,
                "warning": _join(*horse_notes),
            }
        )
    return rows, notes


def _diff_bets(
    left: dict[str, Any],
    right: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    before, before_notes = _bets_by_key(left, "day_before")
    same, same_notes = _bets_by_key(right, "pre_post_45m")
    notes = before_notes + same_notes
    added = [same[key] for key in same if key not in before]
    removed = [before[key] for key in before if key not in same]
    changed: list[dict[str, Any]] = []
    for key in before:
        if key not in same:
            continue
        fields = [name for name in _BET_FIELDS if before[key].get(name) != same[key].get(name)]
        if not fields:
            continue
        changed.append(
            {
                "ticket_type": key[0],
                "combination": list(key[1]),
                "fields": fields,
                "day_before": {name: before[key].get(name) for name in fields},
                "pre_post_45m": {name: same[key].get(name) for name in fields},
            }
        )
    return {"added": added, "removed": removed, "changed": changed}, notes


def _diff_profit(
    left: dict[str, Any],
    right: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    before, before_note = _profit(left, "day_before")
    same, same_note = _profit(right, "pre_post_45m")
    delta = None
    if before is not None and same is not None:
        delta = same["profit_yen"] - before["profit_yen"]
    return (
        {
            "day_before_stake_yen": None if before is None else before["stake_yen"],
            "day_before_payout_yen": None if before is None else before["payout_yen"],
            "day_before_profit_yen": None if before is None else before["profit_yen"],
            "pre_post_45m_stake_yen": None if same is None else same["stake_yen"],
            "pre_post_45m_payout_yen": None if same is None else same["payout_yen"],
            "pre_post_45m_profit_yen": None if same is None else same["profit_yen"],
            "profit_delta_yen": delta,
        },
        _join(before_note, same_note),
    )


def _profit(record: dict[str, Any], edition: str) -> tuple[dict[str, int] | None, str | None]:
    result = record.get("result")
    if not isinstance(result, dict) or result.get("status") != "settled":
        return None, f"{edition} edition was not settled"
    settlement = result.get("settlement")
    if not isinstance(settlement, dict):
        return None, f"{edition} settlement total was null"
    stake = settlement.get("stake_total_yen")
    payout = settlement.get("payout_total_yen")
    if not _is_int(stake) or not _is_int(payout):
        return None, f"{edition} settlement total was null"
    return {"stake_yen": stake, "payout_yen": payout, "profit_yen": payout - stake}, None


@dataclass
class _RoiLine:
    day: str | None
    bet_type: str | None
    surface: str | None
    odds_band: str | None
    stake: int
    payout: int
    hit: bool | None
    refunded: bool
    warning: str | None


def _roi_lines(
    record: dict[str, Any],
    edges: list[tuple[Decimal, str]],
) -> tuple[list[_RoiLine], list[str]]:
    notes: list[str] = []
    result = record.get("result")
    identity = record.get("record_id")
    if not isinstance(result, dict) or result.get("status") != "settled":
        if _has_bought(record):
            notes.append(f"record was not settled: {identity}")
        return [], notes
    settlement = _as_dict(result.get("settlement"))
    settled_bets = _as_list(settlement.get("bets"))
    race = _as_dict(record.get("race"))
    bets = _as_list(record.get("bets"))
    lines: list[_RoiLine] = []
    for index, bet in enumerate(bets):
        if not isinstance(bet, dict) or bet.get("status") != "bought":
            continue
        if index >= len(settled_bets) or not isinstance(settled_bets[index], dict):
            notes.append(f"hit was not settled: {identity} {bet.get('ticket_type')}")
            continue
        line = settled_bets[index]
        stake = bet.get("stake_yen")
        payout = line.get("payout_yen")
        refunded = line.get("refunded") is True
        hit = line.get("hit")
        ticket = bet.get("ticket_type")
        label = f"{identity} {ticket} {_combo(bet.get('combination'))}"
        if not _is_int(stake):
            notes.append(f"stake_yen was not on the record: {label}")
            continue
        if not _is_int(payout):
            notes.append(f"payout was not settled: {label}")
            continue
        if not isinstance(hit, bool) and not refunded:
            notes.append(f"hit was not settled: {label}")
            continue
        band, band_note = _odds_band(bet, edges)
        if band_note:
            notes.append(f"{band_note}: {label}")
        lines.append(
            _RoiLine(
                day=race.get("date") if isinstance(race.get("date"), str) else None,
                bet_type=ticket if isinstance(ticket, str) else None,
                surface=race.get("surface") if isinstance(race.get("surface"), str) else None,
                odds_band=band,
                stake=stake,
                payout=payout,
                hit=hit if isinstance(hit, bool) else None,
                refunded=refunded,
                warning=band_note,
            )
        )
    return lines, notes


def _roi_group(keys: list[str], key: tuple[Any, ...], lines: list[_RoiLine]) -> dict[str, Any]:
    stake = sum(line.stake for line in lines)
    returned = sum(line.payout for line in lines)
    refunds = sum(line.payout for line in lines if line.refunded)
    hits = sum(1 for line in lines if line.hit is True)
    refund_count = sum(1 for line in lines if line.refunded)
    decided = len(lines) - refund_count
    notes = [line.warning for line in lines if line.warning]
    hit_rate: float | None
    if decided <= 0:
        hit_rate = None
        notes.append("no decided bets")
    else:
        hit_rate = _q4(Decimal(hits) / Decimal(decided))
    roi: float | None
    if stake == 0:
        roi = None
        notes.append("no stake on bought bets")
    else:
        roi = _q4(Decimal(returned) / Decimal(stake) - 1)
    group: dict[str, Any] = {name: key[index] for index, name in enumerate(keys)}
    group.update(
        {
            "bet_count": len(lines),
            "hit_count": hits,
            "refund_count": refund_count,
            "stake_yen": stake,
            "return_yen": returned,
            "refund_yen": refunds,
            "profit_yen": returned - stake,
            "hit_rate": hit_rate,
            "roi": roi,
            "warning": _join(*notes),
        }
    )
    return group


def _group_key(line: _RoiLine, keys: list[str]) -> tuple[Any, ...]:
    values = {
        "day": line.day,
        "bet_type": line.bet_type,
        "surface": line.surface,
        "odds_band": line.odds_band,
    }
    return tuple(values[key] for key in keys)


def _odds_band(
    bet: dict[str, Any], edges: list[tuple[Decimal, str]]
) -> tuple[str | None, str | None]:
    odds = bet.get("odds")
    if not _is_number(odds):
        if bet.get("ticket_type") == "wide":
            return None, WIDE_ODDS_RANGE
        return None, "odds were not on the bet"
    number = Decimal(str(odds))
    if number < edges[0][0]:
        return f"<{edges[0][1]}", None
    for index, (_edge, label) in enumerate(edges):
        nxt = edges[index + 1][0] if index + 1 < len(edges) else None
        if nxt is None or number < nxt:
            if nxt is None:
                return f"{label}+", None
            return f"{label}-{edges[index + 1][1]}", None
    return f"{edges[-1][1]}+", None


def _bets_by_key(
    record: dict[str, Any],
    edition: str,
) -> tuple[dict[tuple[str, tuple[int, ...]], dict[str, Any]], list[str]]:
    found: dict[tuple[str, tuple[int, ...]], dict[str, Any]] = {}
    dropped: set[tuple[str, tuple[int, ...]]] = set()
    notes: list[str] = []
    bets = _as_list(record.get("bets"))
    for bet in bets:
        if not isinstance(bet, dict):
            continue
        ticket = bet.get("ticket_type")
        combination = bet.get("combination")
        if not isinstance(ticket, str) or not isinstance(combination, list):
            notes.append(f"bet was not on the record: {edition}")
            continue
        key = (ticket, tuple(combination))
        if key in dropped:
            continue
        if key in found:
            notes.append(f"duplicate bet on {edition}: {ticket} {_combo(combination)}")
            found.pop(key)
            dropped.add(key)
            continue
        found[key] = bet
    return found, notes


def _horses_by_number(record: dict[str, Any]) -> dict[int, dict[str, Any]]:
    horses = _as_list(record.get("horses"))
    found: dict[int, dict[str, Any]] = {}
    for horse in horses:
        if isinstance(horse, dict) and isinstance(horse.get("horse_number"), int):
            found[horse["horse_number"]] = horse
    return found


def _edition_rows(records: list[dict[str, Any]], edition: str) -> list[dict[str, Any]]:
    rows = [record for record in records if _edition(record) == edition]
    return sorted(rows, key=lambda record: str(_run(record).get("run_at") or ""))


def _unpaired(record: dict[str, Any], warning: str) -> dict[str, Any]:
    ref = _run_ref(record)
    ref["warning"] = warning
    return ref


def _run_ref(record: dict[str, Any]) -> dict[str, Any]:
    run = _run(record)
    return {
        "record_id": record.get("record_id"),
        "race_id": _race_id(record),
        "edition": run.get("edition"),
        "run_at": run.get("run_at"),
    }


def _has_bought(record: dict[str, Any]) -> bool:
    bets = _as_list(record.get("bets"))
    return any(isinstance(bet, dict) and bet.get("status") == "bought" for bet in bets)


def _race_id(record: dict[str, Any]) -> str | None:
    race = record.get("race")
    if isinstance(race, dict) and isinstance(race.get("race_id"), str):
        return race["race_id"]
    return None


def _run(record: dict[str, Any]) -> dict[str, Any]:
    run = record.get("run")
    return run if isinstance(run, dict) else {}


def _edition(record: dict[str, Any]) -> str | None:
    edition = _run(record).get("edition")
    return edition if isinstance(edition, str) else None


def _combo(numbers: Any) -> str:
    if not isinstance(numbers, list):
        return ""
    return "-".join(str(number) for number in numbers)


def _delta(left: Any, right: Any) -> float | None:
    if not _is_number(left) or not _is_number(right):
        return None
    value = Decimal(str(right)) - Decimal(str(left))
    return float(value.quantize(_DELTA, rounding=ROUND_HALF_UP))


def _q(value: Decimal) -> float:
    return float(value)


def _q4(value: Decimal) -> float:
    return float(value.quantize(_QUANT, rounding=ROUND_HALF_UP))


def _is_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _plain(value: Any) -> str:
    if value is None:
        return "-"
    return str(value)


def _join(*parts: str | None) -> str | None:
    found: list[str] = []
    for part in parts:
        if not part:
            continue
        for piece in part.split("; "):
            text = piece.strip()
            if text and text not in found:
                found.append(text)
    if not found:
        return None
    return "; ".join(found)


def unique_race_ids(records: Iterable[dict[str, Any]]) -> list[str]:
    """Race ids in first-seen order."""
    seen: list[str] = []
    for record in records:
        race_id = _race_id(record)
        if race_id and race_id not in seen:
            seen.append(race_id)
    return seen
