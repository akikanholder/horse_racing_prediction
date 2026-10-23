"""JSONL store of prediction records, joined later with settled results.

Prediction fields are written once. ``settle`` replaces only ``result``, and
only when the existing result fetch is confirmed and complete. A refused fetch
is not retried.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Callable
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend.models.race import RacePayout, RaceRefund, SettledFinisher, SettledRaceResult
from backend.services.prediction_validate import (
    DuplicateRecordError,
    PredictionValidationError,
    parse_meeting_date,
    parse_prediction_payload,
    resolve_predictions_dir,
    validate_prediction,
)

logger = logging.getLogger(__name__)

TOKYO = ZoneInfo("Asia/Tokyo")
REFUND_NOT_NON_RUNNER = "refund horses are not scratched or excluded"
_PAYOUT_TICKET = {
    "単勝": "win",
    "ワイド": "wide",
    "馬連": "quinella",
    "3連複": "trio",
    "三連複": "trio",
}
_TICKET_LABELS = {
    "win": frozenset({"単勝"}),
    "wide": frozenset({"ワイド"}),
    "quinella": frozenset({"馬連"}),
    "trio": frozenset({"3連複", "三連複"}),
}
_NON_RUNNER = frozenset({"scratched", "excluded"})
_COMBO = re.compile(r"^\d{1,2}(?:-\d{1,2})*$")
FetchResult = Callable[[str], SettledRaceResult]
Clock = Callable[[], datetime]


class PredictionStore:
    """One directory of ``YYYY-MM-DD.jsonl`` meeting files."""

    def __init__(self, directory: str | Path | None = None):
        self.directory = resolve_predictions_dir(directory)

    def path_for(self, day: date) -> Path:
        return self.directory / f"{day.isoformat()}.jsonl"

    def read(self, day: date) -> list[dict[str, Any]]:
        """Records in file order. A missing file is an empty day."""
        path = self.path_for(day)
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PredictionValidationError(f"{path.name}:{lineno}: invalid JSON") from exc
            if not isinstance(value, dict):
                raise PredictionValidationError(f"{path.name}:{lineno}: line is not an object")
            records.append(value)
        return records

    def add(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Validate and append. A duplicate record_id writes nothing."""
        if not records:
            raise PredictionValidationError("prediction file is empty")
        canonical = [validate_prediction(record, pending_only=True) for record in records]
        seen_ids: set[str] = set()
        for record in canonical:
            if record["record_id"] in seen_ids:
                raise DuplicateRecordError(f"duplicate record_id: {record['record_id']}")
            seen_ids.add(record["record_id"])
        grouped: dict[date, list[dict[str, Any]]] = {}
        for record in canonical:
            day = date.fromisoformat(record["race"]["date"])
            grouped.setdefault(day, []).append(record)
        staged: dict[date, list[dict[str, Any]]] = {}
        for day, incoming in grouped.items():
            existing = self.read(day)
            seen = [record["record_id"] for record in existing]
            for record in incoming:
                if record["record_id"] in seen:
                    raise DuplicateRecordError(f"duplicate record_id: {record['record_id']}")
                seen.append(record["record_id"])
            staged[day] = existing + incoming
        for day, lines in staged.items():
            _atomic_write(self.path_for(day), lines)
        return canonical

    def add_text(self, text: str) -> list[dict[str, Any]]:
        return self.add(parse_prediction_payload(text))

    def show(self, day: date) -> dict[str, Any]:
        return {
            "date": day.isoformat(),
            "path": str(self.path_for(day)),
            "records": self.read(day),
        }

    def settle(
        self,
        day: date,
        fetch: FetchResult,
        *,
        now: Clock | None = None,
    ) -> dict[str, Any]:
        """Fill pending rows whose race is confirmed and complete.

        Each race is fetched once. A row that is not final is left as written
        and listed under ``pending``. Already settled rows are not fetched again.
        """
        records = self.read(day)
        clock = now or datetime.now
        filled_at = _filled_at(clock())
        cache: dict[str, SettledRaceResult | None] = {}
        changed = False
        settled: list[dict[str, str]] = []
        pending: list[dict[str, Any]] = []
        updated_rows: list[dict[str, Any]] = []
        for record in records:
            outcome = _settle_record(record, fetch, cache, filled_at)
            updated_rows.append(outcome.record)
            if outcome.settled_id is not None:
                changed = True
                settled.append(outcome.settled_id)
            if outcome.pending is not None:
                pending.append(outcome.pending)
        if changed:
            _atomic_write(self.path_for(day), updated_rows)
        return {
            "date": day.isoformat(),
            "path": str(self.path_for(day)),
            "settled": settled,
            "pending": pending,
        }


class _Outcome:
    def __init__(
        self,
        record: dict[str, Any],
        settled_id: dict[str, str] | None,
        pending: dict[str, Any] | None,
    ):
        self.record = record
        self.settled_id = settled_id
        self.pending = pending


def _settle_record(
    record: dict[str, Any],
    fetch: FetchResult,
    cache: dict[str, SettledRaceResult | None],
    filled_at: str,
) -> _Outcome:
    identity = _identity(record)
    result = record.get("result")
    if isinstance(result, dict) and result.get("status") == "settled":
        return _Outcome(record, None, None)
    if not isinstance(result, dict) or result.get("status") != "pending":
        return _Outcome(record, None, _pending(identity, None, "result was not pending"))
    if identity.race_id is None:
        return _Outcome(record, None, _pending(identity, None, "prediction record is missing race_id"))
    fetched = _cached_fetch(identity.race_id, fetch, cache)
    if fetched is None:
        return _Outcome(record, None, _pending(identity, None, "race result fetch failed"))
    if fetched.race_id != identity.race_id:
        return _Outcome(record, None, _pending(identity, fetched, "result race_id does not match"))
    if fetched.result_phase != "confirmed" or fetched.complete is not True:
        return _Outcome(record, None, _pending(identity, fetched, None))
    try:
        built = _build_result(record, fetched, filled_at)
        updated = json.loads(json.dumps(record, ensure_ascii=False))
        updated["result"] = built
        validate_prediction(updated, pending_only=False)
        _assert_prediction_unchanged(record, updated)
    except PredictionValidationError as exc:
        logger.warning("prediction settle left %s pending: %s", identity.record_id, exc)
        return _Outcome(record, None, _pending(identity, fetched, str(exc)))
    settled_id = None
    if identity.record_id is not None and identity.race_id is not None:
        settled_id = {"record_id": identity.record_id, "race_id": identity.race_id}
    return _Outcome(updated, settled_id, None)


class _Identity:
    def __init__(self, record_id: str | None, race_id: str | None):
        self.record_id = record_id
        self.race_id = race_id


def _identity(record: dict[str, Any]) -> _Identity:
    record_id = record.get("record_id")
    race = record.get("race")
    race_id = race.get("race_id") if isinstance(race, dict) else None
    return _Identity(
        record_id if isinstance(record_id, str) else None,
        race_id if isinstance(race_id, str) else None,
    )


def _cached_fetch(
    race_id: str,
    fetch: FetchResult,
    cache: dict[str, SettledRaceResult | None],
) -> SettledRaceResult | None:
    if race_id in cache:
        return cache[race_id]
    try:
        fetched = fetch(race_id)
    except Exception:
        logger.exception("prediction settle fetch failed for %s", race_id)
        cache[race_id] = None
        return None
    if not isinstance(fetched, SettledRaceResult):
        logger.warning("prediction settle fetch for %s did not return a settled result", race_id)
        cache[race_id] = None
        return None
    cache[race_id] = fetched
    return fetched


def _pending(
    identity: _Identity,
    fetched: SettledRaceResult | None,
    extra: str | None,
) -> dict[str, Any]:
    phase = None
    complete = False
    warning = extra
    if fetched is not None:
        phase = fetched.result_phase
        complete = fetched.complete
        warning = _join(fetched.warning, extra)
    return {
        "record_id": identity.record_id,
        "race_id": identity.race_id,
        "result_phase": phase,
        "complete": complete,
        "warning": warning,
    }


def _build_result(
    record: dict[str, Any],
    fetched: SettledRaceResult,
    filled_at: str,
) -> dict[str, Any]:
    finishes = _finishes(fetched.results)
    payouts, payout_warnings = _payouts(fetched.payouts)
    warnings = list(payout_warnings)
    by_key: dict[tuple[str, tuple[int, ...]], list[int]] = {}
    present_types: set[str] = set()
    for payout in payouts:
        key = (payout["ticket_type"], tuple(payout["combination"]))
        present_types.add(payout["ticket_type"])
        by_key.setdefault(key, []).append(payout["yen_per_100"])
    settled_bets: list[dict[str, Any]] = []
    bought_stakes: list[int | None] = []
    bought_payouts: list[int | None] = []
    for index, bet in enumerate(record["bets"]):
        line, line_warnings = _settle_bet(index, bet, finishes, fetched.refunds, by_key, present_types)
        settled_bets.append(line)
        warnings.extend(line_warnings)
        if bet["status"] == "bought":
            bought_stakes.append(bet["stake_yen"])
            bought_payouts.append(line["payout_yen"])
    stake_total, payout_total, roi, total_warnings = _totals(bought_stakes, bought_payouts)
    warnings.extend(total_warnings)
    return {
        "status": "settled",
        "result_phase": fetched.result_phase,
        "complete": True,
        "filled_at": filled_at,
        "finishes": finishes,
        "payouts": payouts,
        "settlement": {
            "bets": settled_bets,
            "stake_total_yen": stake_total,
            "payout_total_yen": payout_total,
            "roi": roi,
        },
        "warning": _join(fetched.warning, _join(*warnings)),
    }


def _finishes(rows: list[SettledFinisher]) -> list[dict[str, Any]]:
    ranks = [row.rank for row in rows if row.status == "finished" and row.rank is not None]
    tied = {rank for rank in ranks if ranks.count(rank) > 1}
    finishes: list[dict[str, Any]] = []
    for row in rows:
        dead_heat = bool(
            row.status == "finished" and row.rank is not None and row.rank in tied
        )
        finishes.append(
            {
                "horse_number": row.horse_number,
                "rank": row.rank,
                "status": row.status,
                "dead_heat": dead_heat,
            }
        )
    return finishes


def _payouts(rows: list[RacePayout]) -> tuple[list[dict[str, Any]], list[str]]:
    payouts: list[dict[str, Any]] = []
    warnings: list[str] = []
    for row in rows:
        ticket = _PAYOUT_TICKET.get(row.bet_type)
        if ticket is None:
            continue
        numbers = _combination_numbers(row.combination)
        if numbers is None:
            warnings.append(
                f"payout combination was not horse numbers: {row.bet_type} {row.combination}"
            )
            continue
        length = {"win": 1, "wide": 2, "quinella": 2, "trio": 3}[ticket]
        if len(numbers) != length:
            warnings.append(
                f"payout combination was not horse numbers: {row.bet_type} {row.combination}"
            )
            continue
        payouts.append(
            {
                "ticket_type": ticket,
                "combination": numbers,
                "yen_per_100": row.payout,
            }
        )
    return payouts, warnings


def _combination_numbers(text: str) -> list[int] | None:
    if _COMBO.fullmatch(text) is None:
        return None
    numbers: list[int] = []
    for part in text.split("-"):
        number = int(part)
        if not 1 <= number <= 18 or number in numbers:
            return None
        numbers.append(number)
    return sorted(numbers)


def _settle_bet(
    index: int,
    bet: dict[str, Any],
    finishes: list[dict[str, Any]],
    refunds: list[RaceRefund] | None,
    by_key: dict[tuple[str, tuple[int, ...]], list[int]],
    present_types: set[str],
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    refunded, flagged = _refunded(bet, finishes, refunds)
    if flagged:
        names = "、".join(str(number) for number in flagged)
        warnings.append(f"{REFUND_NOT_NON_RUNNER}: {names}")
    if refunded:
        payout_yen, stake_warning = _refund_payout(bet.get("stake_yen"))
        if stake_warning:
            warnings.append(stake_warning)
        return _line(index, False, True, payout_yen), warnings
    key = (bet["ticket_type"], tuple(bet["combination"]))
    amounts = by_key.get(key, [])
    if amounts:
        unique = set(amounts)
        label = _combo_text(bet["combination"])
        if len(unique) > 1:
            warnings.append(f"payout rows disagree for {bet['ticket_type']} {label}")
            return _line(index, True, False, None), warnings
        payout_yen, scale_warning = _scale_payout(unique.pop(), bet.get("stake_yen"))
        if scale_warning:
            warnings.append(scale_warning)
        return _line(index, True, False, payout_yen), warnings
    if bet["ticket_type"] not in present_types:
        warnings.append(f"{bet['ticket_type']} payout was not on the result")
        return _line(index, None, False, None), warnings
    return _line(index, False, False, 0), warnings


def _line(index: int, hit: bool | None, refunded: bool, payout_yen: int | None) -> dict[str, Any]:
    return {"index": index, "hit": hit, "refunded": refunded, "payout_yen": payout_yen}


def _refunded(
    bet: dict[str, Any],
    finishes: list[dict[str, Any]],
    refunds: list[RaceRefund] | None,
) -> tuple[bool, list[int]]:
    by_number = {
        row["horse_number"]: row for row in finishes if row["horse_number"] is not None
    }
    for number in bet["combination"]:
        row = by_number.get(number)
        if row is not None and row["status"] in _NON_RUNNER:
            return True, []
    if not refunds:
        return False, []
    flagged: list[int] = []
    matched = False
    labels = _TICKET_LABELS[bet["ticket_type"]]
    for block in refunds:
        overlap = [number for number in bet["combination"] if number in block.horse_numbers]
        if not overlap or not _refund_covers(block, labels):
            continue
        matched = True
        for number in overlap:
            row = by_number.get(number)
            if row is None or row["status"] not in _NON_RUNNER:
                if number not in flagged:
                    flagged.append(number)
    return matched, flagged


def _refund_covers(block: RaceRefund, labels: frozenset[str]) -> bool:
    """A null bet list covers every ticket. An empty list names none."""
    if block.bet_types is None:
        return True
    return any(label in block.bet_types for label in labels)


def _refund_payout(stake: Any) -> tuple[int | None, str | None]:
    """返還 returns the stake. The result page does not print a refund price."""
    if isinstance(stake, int) and not isinstance(stake, bool) and stake >= 0:
        return stake, None
    return None, "stake_yen was not on the record"


def _scale_payout(yen_per_100: int, stake: Any) -> tuple[int | None, str | None]:
    if not isinstance(stake, int) or isinstance(stake, bool) or stake < 0:
        return None, "stake_yen was not on the record"
    product = yen_per_100 * stake
    if product % 100 != 0:
        return None, "payout is not an integer yen"
    return product // 100, None


def _totals(
    stakes: list[int | None],
    payouts: list[int | None],
) -> tuple[int | None, int | None, float | None, list[str]]:
    if not stakes:
        return 0, 0, None, ["no bought bets"]
    warnings: list[str] = []
    if any(stake is None for stake in stakes):
        stake_total: int | None = None
        warnings.append("stake_yen was not on the record")
    else:
        stake_total = sum(stake for stake in stakes if stake is not None)
    if any(payout is None for payout in payouts):
        payout_total: int | None = None
    else:
        payout_total = sum(payout for payout in payouts if payout is not None)
    if stake_total is None or payout_total is None:
        return stake_total, payout_total, None, warnings
    if stake_total == 0:
        warnings.append("no stake on bought bets")
        return stake_total, payout_total, None, warnings
    quantized = (Decimal(payout_total) / Decimal(stake_total) - 1).quantize(
        Decimal("0.0001"),
        rounding=ROUND_HALF_UP,
    )
    return stake_total, payout_total, float(quantized), warnings


def _combo_text(numbers: list[int]) -> str:
    return "-".join(str(number) for number in numbers)


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


def _filled_at(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=TOKYO)
    else:
        moment = moment.astimezone(TOKYO)
    return moment.replace(microsecond=0).isoformat()


def _assert_prediction_unchanged(before: dict[str, Any], after: dict[str, Any]) -> None:
    left = {key: value for key, value in before.items() if key != "result"}
    right = {key: value for key, value in after.items() if key != "result"}
    if left != right:
        raise RuntimeError("prediction fields changed during settle")


def _atomic_write(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records
    )
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


__all__ = [
    "DuplicateRecordError",
    "PredictionStore",
    "PredictionValidationError",
    "parse_meeting_date",
    "parse_prediction_payload",
    "resolve_predictions_dir",
    "validate_prediction",
]
