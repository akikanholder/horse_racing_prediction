"""Running style derived from netkeiba passing order.

脚質のページ項目は馬ページにも成績ページにも無い。
成績の「ペース」はレースのラップ（例: ``37.1-33.4``）であり、逃げ・先行・差し・追込ではない。
ハイ/スローへは分類しない。オッズ、人気、レース名からは作らない。
"""

from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, Field

STYLE_LABELS: tuple[str, ...] = ("逃げ", "先行", "差し", "追込")
StyleLabel = Literal["逃げ", "先行", "差し", "追込"]
DataSufficiency = Literal["sufficient", "thin", "insufficient"]

STYLE_BASIS = "passing_order_first_call"
STYLE_PASSING_UNAVAILABLE = "passing order unavailable"
STYLE_FIELD_SIZE_UNAVAILABLE = "running style field size unavailable"
STYLE_SAMPLE_TOO_SMALL = "running style sample too small"
STYLE_TIE = "running style tie"

SHORT_DISTANCE_M = 1400
MIN_RUNS_FOR_LABEL = 3
MIN_RUNS_FOR_SUFFICIENT = 5
MIN_CONFIDENCE_FOR_SUFFICIENT = 0.6
MIN_SHORT_FRONT_RUNS = 3

_FRONT_LABELS = frozenset({"逃げ", "先行"})


class EarlyCallRun(Protocol):
    """Fields the classifier is allowed to read."""

    passing_order: str | None
    entry_count: int | None
    distance: int | None
    track_type: str | None


class RunningStyle(BaseModel):
    """通過順の最初の数字から集計した脚質。

    ``label`` は判定できた走が3走未満、または最多が同数のときは null。
    その内訳は ``counts`` に残す。1走だけの逃げを脚質としては出さない。
    ``short_distance_front_under_specified`` は 1400m 以下の逃げ・先行が
    1走以上3走未満のとき true。全体の ``label`` は消さない。
    """

    label: StyleLabel | None = Field(
        None, description="逃げ / 先行 / 差し / 追込。根拠が薄いときは null"
    )
    counts: dict[str, int] = Field(
        default_factory=lambda: dict.fromkeys(STYLE_LABELS, 0),
        description="判定できた走の内訳。逃げ・先行・差し・追込",
    )
    sample_size: int = Field(0, description="脚質を付けられた走数。通過順か頭数が無い走は含めない")
    unusable_runs: int = Field(0, description="通過順か頭数が無く、脚質を付けなかった走数")
    confidence: float | None = Field(
        None, description="label の占有率。label が null のときは null"
    )
    data_sufficiency: DataSufficiency = Field(
        "insufficient",
        description="sufficient / thin / insufficient",
    )
    basis: str | None = Field(
        None, description="passing_order_first_call。1走も判定できないときは null"
    )
    short_distance_m: int = Field(
        SHORT_DISTANCE_M, description="短距離とみなす上限（メートル）。1400m 以下"
    )
    short_distance_runs: int = Field(0, description="脚質を付けた短距離の走数。障害は含めない")
    short_distance_front_runs: int = Field(0, description="短距離で逃げまたは先行だった走数")
    short_distance_front_under_specified: bool = Field(
        False,
        description="短距離の逃げ・先行が1〜2走のとき true。EV が感度を上げるための印",
    )
    warning: str | None = Field(
        None,
        description=(
            "label を出さない理由。"
            "passing order unavailable / running style field size unavailable / "
            "running style sample too small / running style tie"
        ),
    )


def empty_counts() -> dict[str, int]:
    return dict.fromkeys(STYLE_LABELS, 0)


def classify_early_call(position: int, field_size: int) -> StyleLabel | None:
    """Map the first passing position to one style.

    4区分。通過の最初の数字がそのレースで最初に記録されたコーナー順位。
    1 は逃げで、三等分には入れない。残りの馬だけを頭数の閾値で分ける。

    - ``front_end = ceil(field_size / 3)``
    - ``mid_end = ceil(field_size * 2 / 3)``
    - 逃げ: ``position == 1``
    - 先行: ``1 < position <= front_end``
    - 差し: ``front_end < position <= mid_end``
    - 追込: ``position > mid_end``

    ``front_end`` が 1 のときは先行は空。頭数より大きい順位、0 以下はラベルにしない。
    """
    if field_size < 1 or position < 1 or position > field_size:
        return None
    if position == 1:
        return "逃げ"
    front_end = (field_size + 2) // 3
    mid_end = (field_size * 2 + 2) // 3
    if position <= front_end:
        return "先行"
    if position <= mid_end:
        return "差し"
    return "追込"


def early_position(passing_order: str | None) -> int | None:
    """First corner call. ``13-12-12-7`` is 13. Anything else is unreadable."""
    if not passing_order:
        return None
    compact = "".join(passing_order.split())
    if not compact:
        return None
    parts = compact.split("-")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    position = int(parts[0])
    if position < 1:
        return None
    return position


def _is_short(distance: int | None, track_type: str | None) -> bool:
    if distance is None or distance <= 0 or distance > SHORT_DISTANCE_M:
        return False
    return track_type != "障害"


def derive_running_style(performances: Sequence[EarlyCallRun]) -> RunningStyle:
    """Aggregate early calls. Does not read odds, popularity, or race name."""
    counts = empty_counts()
    parsed_calls = 0
    sample_size = 0
    short_runs = 0
    short_front = 0

    for perf in performances:
        position = early_position(perf.passing_order)
        if position is None:
            continue
        parsed_calls += 1
        label = classify_early_call(position, perf.entry_count or 0)
        if label is None:
            continue
        counts[label] += 1
        sample_size += 1
        if _is_short(perf.distance, perf.track_type):
            short_runs += 1
            if label in _FRONT_LABELS:
                short_front += 1

    under_specified = 0 < short_front < MIN_SHORT_FRONT_RUNS
    unusable = len(performances) - sample_size
    basis = STYLE_BASIS if sample_size else None

    if sample_size == 0:
        warning = STYLE_FIELD_SIZE_UNAVAILABLE if parsed_calls else STYLE_PASSING_UNAVAILABLE
        return RunningStyle(
            label=None,
            counts=counts,
            sample_size=0,
            unusable_runs=unusable,
            confidence=None,
            data_sufficiency="insufficient",
            basis=None,
            short_distance_runs=0,
            short_distance_front_runs=0,
            short_distance_front_under_specified=False,
            warning=warning,
        )

    ranking = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    top_label, top_count = ranking[0]
    tied = len(ranking) > 1 and ranking[1][1] == top_count and top_count > 0
    if sample_size < MIN_RUNS_FOR_LABEL or tied or top_count == 0:
        return RunningStyle(
            label=None,
            counts=counts,
            sample_size=sample_size,
            unusable_runs=unusable,
            confidence=None,
            data_sufficiency="thin"
            if tied and sample_size >= MIN_RUNS_FOR_LABEL
            else "insufficient",
            basis=basis,
            short_distance_runs=short_runs,
            short_distance_front_runs=short_front,
            short_distance_front_under_specified=under_specified,
            warning=STYLE_TIE
            if tied and sample_size >= MIN_RUNS_FOR_LABEL
            else STYLE_SAMPLE_TOO_SMALL,
        )

    confidence = round(top_count / sample_size, 4)
    if sample_size >= MIN_RUNS_FOR_SUFFICIENT and confidence >= MIN_CONFIDENCE_FOR_SUFFICIENT:
        sufficiency: DataSufficiency = "sufficient"
    else:
        sufficiency = "thin"
    return RunningStyle(
        label=top_label,  # type: ignore[arg-type]
        counts=counts,
        sample_size=sample_size,
        unusable_runs=unusable,
        confidence=confidence,
        data_sufficiency=sufficiency,
        basis=basis,
        short_distance_runs=short_runs,
        short_distance_front_runs=short_front,
        short_distance_front_under_specified=under_specified,
        warning=None,
    )
