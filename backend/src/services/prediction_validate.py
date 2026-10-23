"""Validate hrp.prediction records.

The JSON Schema file is the field contract. This module applies that file and
the checks a schema cannot express: the record id, the weight total, ascending
combinations, and wide prices. ``rules`` is checked only as a JSON object.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend.scrapers.track_freshness import parse_race_day

TOKYO = ZoneInfo("Asia/Tokyo")
PREDICTIONS_DIR_ENV = "HRP_PREDICTIONS_DIR"

_STAMP = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\+09:00$")
_POST = re.compile(
    r"^(?:(\d{4})-(\d{2})-(\d{2})T)?(\d{2}):(\d{2})(?::(\d{2}))?\+09:00$"
)
_TICKET_LENGTH = {"win": 1, "wide": 2, "quinella": 2, "trio": 3}
_MAX_DEPTH = 64


class PredictionStoreError(Exception):
    """Base error for the prediction record store."""


class PredictionValidationError(PredictionStoreError):
    """The record does not match the prediction contract."""


class DuplicateRecordError(PredictionStoreError):
    """A record_id is already stored for that meeting day."""


_SCHEMA_NAME = "hrp.prediction.schema.json"


def schema_path() -> Path:
    """Filesystem path of the prediction JSON Schema.

    Package data is preferred. A source checkout that has no installed copy
    falls back to ``docs/schemas``.
    """
    packaged = _packaged_schema_path()
    if packaged is not None:
        return packaged
    repo = _repo_schema_path()
    if repo is not None:
        return repo
    raise PredictionValidationError(
        "hrp.prediction schema was not found in package data or docs/schemas"
    )


def _packaged_schema_path() -> Path | None:
    """Real file from importlib.resources. Zip extracts are not a stable path."""
    try:
        from importlib.resources import as_file, files

        resource = files("backend.services").joinpath(_SCHEMA_NAME)
        if not resource.is_file():
            return None
        with as_file(resource) as extracted:
            path = Path(extracted)
            if not path.is_file():
                return None
            if path.is_relative_to(Path(tempfile.gettempdir())):
                return None
            return path
    except (FileNotFoundError, ModuleNotFoundError, OSError, TypeError, ValueError):
        return None


def _repo_schema_path() -> Path | None:
    """Walk ancestors so the docs copy is found from a checkout of any depth."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "schemas" / _SCHEMA_NAME
        if candidate.is_file():
            return candidate
    return None


def _schema_text() -> str:
    try:
        from importlib.resources import files

        resource = files("backend.services").joinpath(_SCHEMA_NAME)
        if resource.is_file():
            return resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError, TypeError, ValueError):
        pass
    path = schema_path()
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def prediction_schema() -> dict[str, Any]:
    """Load the prediction JSON Schema once."""
    loaded = json.loads(_schema_text())
    if not isinstance(loaded, dict):
        raise PredictionValidationError("hrp.prediction schema must be an object")
    return loaded


def resolve_predictions_dir(explicit: str | Path | None = None) -> Path:
    """Directory of ``YYYY-MM-DD.jsonl`` files.

    ``--dir`` wins, then ``HRP_PREDICTIONS_DIR``, then ``./predictions``.
    """
    if explicit is not None and str(explicit) != "":
        return Path(explicit)
    env = os.environ.get(PREDICTIONS_DIR_ENV)
    if env:
        return Path(env)
    return Path("predictions")


def parse_meeting_date(value: str) -> date:
    """``YYYYMMDD`` or ``YYYY-MM-DD``. Anything else is rejected."""
    day, warning = parse_race_day(value)
    if day is None or warning:
        raise PredictionValidationError("date must be YYYYMMDD or YYYY-MM-DD")
    return day


def parse_prediction_payload(text: str) -> list[dict[str, Any]]:
    """One JSON object, a JSON array, or JSONL. Blank lines are skipped."""
    stripped = text.strip()
    if not stripped:
        raise PredictionValidationError("prediction file is empty")
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        if not value or not all(isinstance(item, dict) for item in value):
            raise PredictionValidationError("prediction list items must be objects")
        return list(value)
    if value is not None:
        raise PredictionValidationError("prediction file must be an object, a list, or JSONL")
    records: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PredictionValidationError(f"line {lineno}: invalid JSON") from exc
        if not isinstance(item, dict):
            raise PredictionValidationError(f"line {lineno}: must be an object")
        records.append(item)
    if not records:
        raise PredictionValidationError("prediction file is empty")
    return records


def compact_run_at(run_at: str) -> str:
    """``2026-10-18T14:35:00+09:00`` becomes ``20261018T143500+0900``."""
    _real_stamp(run_at, "run.run_at")
    return run_at.replace("-", "").replace(":", "")


def expected_record_id(race_id: str, edition: str, run_at: str) -> str:
    """Key of one race, one edition, and one run."""
    return f"{race_id}_{edition}_{compact_run_at(run_at)}"


def validate_prediction(data: Any, *, pending_only: bool = False) -> dict[str, Any]:
    """Return a JSON copy of a valid record. The input object is not modified.

    ``pending_only`` is for ``add``. A filled result is not accepted there.
    """
    if not isinstance(data, dict):
        raise PredictionValidationError("prediction must be an object")
    if pending_only:
        result = data.get("result")
        if not isinstance(result, dict) or result.get("status") != "pending":
            raise PredictionValidationError("result must be pending when adding a prediction")
    _assert_json_value(data, "$", 0)
    _validate_schema(data, prediction_schema(), "$", 0)
    _cross_check(data, pending_only=pending_only)
    return json.loads(json.dumps(data, ensure_ascii=False))


def _cross_check(record: dict[str, Any], *, pending_only: bool) -> None:
    race = record["race"]
    run = record["run"]
    _real_date(race["date"], "race.date")
    _check_post_time(race["post_time"])
    _real_stamp(run["run_at"], "run.run_at")
    expected = expected_record_id(race["race_id"], run["edition"], run["run_at"])
    if record["record_id"] != expected:
        raise PredictionValidationError(f"record_id must be {expected}")
    total = sum((Decimal(str(weight)) for weight in run["weights"]), Decimal(0))
    if total != Decimal(100):
        raise PredictionValidationError("weights must sum to 100")
    seen: set[int] = set()
    for index, horse in enumerate(record["horses"]):
        number = horse["horse_number"]
        if number in seen:
            raise PredictionValidationError(f"horses[{index}] duplicates horse_number {number}")
        seen.add(number)
        _check_horse_times(horse, index)
        span = horse["win_prob_range"]
        if span is not None and span[0] > span[1]:
            raise PredictionValidationError(
                f"horses[{index}].win_prob_range lo must be <= hi"
            )
        prob = horse["win_prob"]
        if span is not None and prob is not None and not span[0] <= prob <= span[1]:
            raise PredictionValidationError(
                f"horses[{index}].win_prob must sit inside win_prob_range"
            )
    for index, source in enumerate(record["sources"]):
        _real_stamp(source["fetched_at"], f"sources[{index}].fetched_at")
        if source["measured_at"] is not None:
            _real_stamp(source["measured_at"], f"sources[{index}].measured_at")
        if source["market_updated_at"] is not None:
            _real_stamp(source["market_updated_at"], f"sources[{index}].market_updated_at")
    for index, bet in enumerate(record["bets"]):
        _check_bet(bet, index)
    result = record["result"]
    if pending_only and result.get("status") != "pending":
        raise PredictionValidationError("result must be pending when adding a prediction")
    if result.get("status") == "settled":
        _real_stamp(result["filled_at"], "result.filled_at")


def _check_horse_times(horse: dict[str, Any], index: int) -> None:
    fetched = horse["odds_fetched_at"]
    if fetched is not None:
        _real_stamp(fetched, f"horses[{index}].odds_fetched_at")


def _check_bet(bet: dict[str, Any], index: int) -> None:
    kind = bet["ticket_type"]
    combination = bet["combination"]
    expected = _TICKET_LENGTH[kind]
    path = f"bets[{index}]"
    if len(combination) != expected:
        raise PredictionValidationError(f"{path} {kind} combination must have {expected} horses")
    if combination != sorted(combination) or len(set(combination)) != len(combination):
        raise PredictionValidationError(f"{path}.combination must be horse numbers ascending")
    if kind == "wide":
        if bet["odds"] is not None:
            raise PredictionValidationError(f"{path} wide odds must be null; use odds_min and odds_max")
        low, high = bet["odds_min"], bet["odds_max"]
        if (low is None) != (high is None):
            raise PredictionValidationError(
                f"{path} wide odds_min and odds_max must both be present or both null"
            )
        if low is not None and low > high:
            raise PredictionValidationError(f"{path} wide odds_min must be <= odds_max")
    elif bet["odds_min"] is not None or bet["odds_max"] is not None:
        raise PredictionValidationError(f"{path} {kind} odds_min and odds_max must be null")
    if bet["status"] == "bought" and bet["stake_yen"] is None:
        raise PredictionValidationError(f"{path} bought stake_yen must be an integer")


def _check_post_time(value: str | None) -> None:
    if value is None:
        return
    match = _POST.fullmatch(value)
    if match is None:
        raise PredictionValidationError("race.post_time must use +09:00")
    year, month, day, hour, minute, second = match.groups()
    clock = (int(hour), int(minute), int(second) if second is not None else 0)
    if not (0 <= clock[0] <= 23 and 0 <= clock[1] <= 59 and 0 <= clock[2] <= 59):
        raise PredictionValidationError("race.post_time must be a real time")
    if year is None:
        return
    try:
        datetime(int(year), int(month), int(day), *clock, tzinfo=TOKYO)
    except ValueError as exc:
        raise PredictionValidationError("race.post_time must be a real time") from exc


def _real_date(value: str, path: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise PredictionValidationError(f"{path} must be a calendar date") from exc


def _real_stamp(value: str, path: str) -> None:
    match = _STAMP.fullmatch(value)
    if match is None:
        raise PredictionValidationError(f"{path} must be YYYY-MM-DDTHH:MM:SS+09:00")
    parts = tuple(int(group) for group in match.groups())
    try:
        datetime(*parts, tzinfo=TOKYO)
    except ValueError as exc:
        raise PredictionValidationError(f"{path} must be a real time") from exc


def _assert_json_value(value: Any, path: str, depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise PredictionValidationError(f"{path} is nested too deeply")
    if value is None or isinstance(value, str):
        return
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PredictionValidationError(f"{path} must be a finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_json_value(item, f"{path}[{index}]", depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PredictionValidationError(f"{path} keys must be strings")
            _assert_json_value(item, f"{path}.{key}", depth + 1)
        return
    raise PredictionValidationError(f"{path} is not a JSON value")


def _validate_schema(instance: Any, schema: dict[str, Any], path: str, depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise PredictionValidationError(f"{path} is nested too deeply")
    if "anyOf" in schema:
        errors: list[str] = []
        for option in schema["anyOf"]:
            try:
                _validate_schema(instance, option, path, depth + 1)
            except PredictionValidationError as exc:
                errors.append(str(exc))
            else:
                return
        detail = "; ".join(errors) if errors else "no schema"
        raise PredictionValidationError(f"{path} matched no schema: {detail}")
    expected = schema.get("type")
    if expected is not None:
        _check_type(instance, expected, path)
    if "const" in schema and instance != schema["const"]:
        raise PredictionValidationError(f"{path} must be {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        raise PredictionValidationError(f"{path} must be one of {schema['enum']!r}")
    if isinstance(instance, str):
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, instance) is None:
            raise PredictionValidationError(f"{path} has the wrong shape")
        min_length = schema.get("minLength")
        if min_length is not None and len(instance) < min_length:
            raise PredictionValidationError(f"{path} is too short")
        max_length = schema.get("maxLength")
        if max_length is not None and len(instance) > max_length:
            raise PredictionValidationError(f"{path} is too long")
    if _is_number(instance):
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            raise PredictionValidationError(f"{path} must be > {schema['exclusiveMinimum']}")
        if "minimum" in schema and instance < schema["minimum"]:
            raise PredictionValidationError(f"{path} must be >= {schema['minimum']}")
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            raise PredictionValidationError(f"{path} must be < {schema['exclusiveMaximum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            raise PredictionValidationError(f"{path} must be <= {schema['maximum']}")
    if expected == "object":
        if not isinstance(instance, dict):
            raise PredictionValidationError(f"{path} must be an object")
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in instance:
                raise PredictionValidationError(f"{path}.{key} is required")
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            child = f"{path}.{key}"
            if key in properties:
                _validate_schema(value, properties[key], child, depth + 1)
            elif additional is False:
                raise PredictionValidationError(f"{child} is not allowed")
            elif isinstance(additional, dict):
                _validate_schema(value, additional, child, depth + 1)
    if expected == "array":
        if not isinstance(instance, list):
            raise PredictionValidationError(f"{path} must be an array")
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise PredictionValidationError(f"{path} is too short")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise PredictionValidationError(f"{path} is too long")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                _validate_schema(item, item_schema, f"{path}[{index}]", depth + 1)


def _check_type(instance: Any, expected: str, path: str) -> None:
    if expected == "object" and isinstance(instance, dict):
        return
    if expected == "array" and isinstance(instance, list):
        return
    if expected == "string" and isinstance(instance, str):
        return
    if expected == "boolean" and isinstance(instance, bool):
        return
    if expected == "integer" and _is_integer(instance):
        return
    if expected == "number" and _is_number(instance):
        return
    if expected == "null" and instance is None:
        return
    raise PredictionValidationError(f"{path} must be {expected}")


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    if _is_integer(value):
        return True
    return isinstance(value, float) and math.isfinite(value)
