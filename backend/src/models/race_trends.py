"""Settled-race trend tables for EV calibration.

Rates and ROI are computed only from runners that have the printed inputs.
A missing price, sire, or running style stays null and carries a warning.
Buckets with a small sample are kept. Their ``sample_size`` is the count.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from backend.models.running_style import STYLE_BASIS

STAKE_YEN = 100
RECENT_MEETING_LOOKBACK_DAYS = 120
FlatSurface = Literal["芝", "ダート"]
Going = Literal["良", "稍重", "重", "不良"]
TrendMode = Literal["range", "recent_meeting"]

POPULARITY_BANDS: tuple[tuple[str, int, int | None], ...] = (
    ("1", 1, 1),
    ("2", 2, 2),
    ("3", 3, 3),
    ("4-6", 4, 6),
    ("7-9", 7, 9),
    ("10+", 10, None),
)

ROI_BASIS = (
    "(払戻または確定オッズの円 / (100円 × 価格が付いた頭数)) - 1。"
    "0 が元返し。価格が無い頭は分母に入れず、rate は null"
)
PLACE_BASIS = (
    "複勝は出走が8頭以上なら3着まで、5頭以上7頭以下なら2着まで。"
    "4頭以下は発売しないものとして place_roi は null。"
    "複勝オッズが範囲のときは中央値を作らない"
)
FINAL_WIN_ODDS_MISSING = "final win odds were not on the page"
RESULT_PHASE_MISSING = "result phase was not on the page"
RESULT_PHASE_PROVISIONAL = "result phase is provisional"
NO_STARTERS = "no starters in the race"
PLACE_NOT_SOLD = "place betting is not sold when fewer than 5 horses start"
PLACE_FIELD_UNKNOWN = "place roi unavailable: field size was not on the page"
PLACE_PAYOUT_MISSING = "place payout was not on the page"
PLACE_RANGE_UNUSED = "place odds are a range and were not used"
WIN_ODDS_PAYOUT_DISAGREE = "win payout and final win odds disagree"
PLACE_ODDS_PAYOUT_DISAGREE = "place payout and final place odds disagree"
FETCHED_AT_DIFFERS = "fetched_at differs across included races"
RECENT_MEETING_NEEDS_VENUE = "recent meeting requires one venue"
SIRE_UNAVAILABLE = "sire was not on the pedigree page"
SIRE_NOT_LOADED = "sire was not loaded"
HORSE_ID_MISSING = "horse id was not on the page"
JOCKEY_MISSING = "jockey was not on the page"
POPULARITY_MISSING = "popularity was not on the page"
GATE_MISSING = "horse number was not on the page"
FRAME_MISSING = "frame was not on the page"
SURFACE_MISSING = "surface was not on the page"
RANK_MISSING = "rank was not on the page"
SCRATCHED = "scratched or excluded"
EMPTY_POPULARITY_BAND = "no runners in this popularity band"
EMPTY_RUNNING_STYLE = "no runners with this running style"
WIN_ROI_PARTIAL = "win roi omits runners without a usable win return"
PLACE_ROI_PARTIAL = "place roi omits runners without a usable place return"
DATE_RANGE_REQUIRED = "a date or date range is required"
DATE_RANGE_IGNORED = "date range is ignored in recent meeting mode"
AS_OF_IGNORED = "as_of applies only to recent meeting mode"
HISTORICAL_START_WARNING = "historical races start in 2025"
RESULT_NOT_COMPLETE = "race result is not complete"
RACE_NOT_SETTLED = "race not yet settled"
TODAY_CONFLICT = "today is the Asia/Tokyo meeting day and does not take another date"


class TrendFilter(BaseModel):
    """Filters applied to settled races. Unset fields do not filter."""

    venues: list[str] = Field(default_factory=list, description="競馬場。空は指定なし")
    surface: FlatSurface | None = Field(None, description="芝またはダート")
    distance: int | None = Field(None, description="距離 (m)")
    race_class: str | None = Field(None, description="クラス。印字との完全一致")
    going: Going | None = Field(None, description="良 / 稍重 / 重 / 不良")
    date_from: date | None = Field(None, description="開始日。含む")
    date_to: date | None = Field(None, description="終了日。含む")
    recent_meeting: bool = Field(False, description="当日の確定と、同じ競馬場の前開催日だけ")
    as_of: date | None = Field(
        None, description="recent meeting の当日。無ければ今日（Asia/Tokyo）"
    )
    today: bool = Field(
        False,
        description="true のとき開催日は今日（Asia/Tokyo）の1日。date_from と date_to がその日",
    )


class TrendBucket(BaseModel):
    """Win, top-2, top-3, and ROI for one bucket.

    ``sample_size`` が 0 のとき rate と ROI は null。0.0 にはしない。
    """

    sample_size: int = Field(0, description="この区分に数えた出走馬。取消・除外は含めない")
    wins: int = Field(0, description="1着の頭数")
    top2: int = Field(0, description="2着以内の頭数")
    top3: int = Field(0, description="3着以内の頭数")
    win_rate: float | None = Field(None, description="wins / sample_size。0頭のときは null")
    top2_rate: float | None = Field(None, description="top2 / sample_size。0頭のときは null")
    top3_rate: float | None = Field(None, description="top3 / sample_size。0頭のときは null")
    win_roi: float | None = Field(
        None, description="単勝の ROI。(回収 / 投資) - 1。価格が無いときは null"
    )
    place_roi: float | None = Field(
        None, description="複勝の ROI。(回収 / 投資) - 1。払戻が無いときは null"
    )
    win_stake_count: int = Field(0, description="単勝 ROI の分母に入れた頭数")
    place_stake_count: int = Field(0, description="複勝 ROI の分母に入れた頭数")
    warning: str | None = Field(None, description="この区分を計算できない、または一部を除いた理由")


class PopularityBandRow(TrendBucket):
    """One popularity band. Empty bands are kept so a small band is not dropped."""

    band: str = Field(..., description="1 / 2 / 3 / 4-6 / 7-9 / 10+")
    popularity_min: int = Field(..., description="この帯の最小人気")
    popularity_max: int | None = Field(None, description="この帯の最大人気。10+ は null")


class GateRow(TrendBucket):
    """馬番。出た番だけ。1頭でも残す。"""

    horse_number: int = Field(..., description="馬番", ge=1)


class FrameRow(TrendBucket):
    """枠番。出た枠だけ。1頭でも残す。"""

    bracket_number: int = Field(..., description="枠番", ge=1)


class RunningStyleDefinition(BaseModel):
    """How a runner in one race was labeled. Thresholds are the formulas, not a fit."""

    basis: str = Field(..., description="passing_order_first_call")
    position: str = Field(..., description="どの通過順を使うか。4区分")
    escape: str = Field(..., description="逃げ。最初の通過順位が 1")
    front_end: str = Field(..., description="先行。1 以外で ceil(field_size / 3) まで")
    mid_end: str = Field(..., description="差し。1 以外で ceil(field_size * 2 / 3) まで")
    closer: str = Field(..., description="追込。差しの上限より後ろ")
    unusable: str = Field(..., description="ラベルにしない走と、少ない標本の扱い")


class RunningStyleRow(TrendBucket):
    """逃げ / 先行 / 差し / 追込。0頭でも行は残す。"""

    label: str = Field(..., description="逃げ / 先行 / 差し / 追込")


class JockeySurfaceRow(TrendBucket):
    """騎手 × 芝/ダート。面は混ぜない。1鞍でも残す。"""

    jockey_name: str = Field(..., description="騎手名。ページの文字")
    surface: FlatSurface = Field(..., description="芝またはダート")


class SireSurfaceRow(TrendBucket):
    """父 × 芝/ダート。父が取れない馬は行にしない。"""

    sire_name: str | None = Field(None, description="父名。ページに無いときは null")
    sire_id: str | None = Field(None, description="父の馬ID。リンクが無いときは null")
    surface: FlatSurface = Field(..., description="芝またはダート")
    pedigree_source: str | None = Field(
        None, description="profile / netkeiba_ped / profile+netkeiba_ped。混ざるときは + "
    )


class ExcludedRace(BaseModel):
    """A race that was in the fetch but not in the rates."""

    race_id: str = Field(..., description="レースID")
    reason: str = Field(..., description="集計に入れなかった理由")


class IncludedRace(BaseModel):
    """One race that was counted. source and fetched_at are that race's own."""

    race_id: str = Field(..., description="レースID")
    source: str | None = Field(None, description="そのレースの source。無ければ null")
    fetched_at: datetime = Field(..., description="そのレースの fetched_at")


class RunnerOmission(BaseModel):
    """Runners inside included races that were left out of one table."""

    reason: str = Field(..., description="その表に入れなかった理由")
    count: int = Field(..., description="頭数", ge=0)


class RecentMeetingWindow(BaseModel):
    """Today's confirmed races plus the previous meeting day at one venue."""

    venue: str | None = Field(None, description="指定した競馬場")
    as_of: date | None = Field(None, description="当日")
    previous_meeting_date: date | None = Field(
        None, description="同じ競馬場で、当日より前の直近の開催日。見つからなければ null"
    )
    lookback_days: int = Field(RECENT_MEETING_LOOKBACK_DAYS, description="前開催日を探す最大日数")
    warning: str | None = Field(None, description="前開催日が取れない理由")


class RaceTrendReport(BaseModel):
    """Trend tables over settled races already stored by history."""

    mode: TrendMode = Field(..., description="range または recent_meeting")
    filter: TrendFilter = Field(..., description="指定した条件")
    recent_meeting: RecentMeetingWindow | None = Field(
        None, description="recent meeting のときだけ。range では null"
    )
    sample_size: int = Field(0, description="集計に入れたレース数")
    runner_sample_size: int = Field(0, description="集計に入れた出走馬数。取消・除外は含めない")
    included_races: list[IncludedRace] = Field(
        default_factory=list,
        description="集計に入れたレース。各行に race_id と source と fetched_at",
    )
    included_race_ids: list[str] = Field(
        default_factory=list, description="included_races の race_id。同じ順"
    )
    races_pending: int = Field(
        0,
        description=(
            "最終結果ではないレース数。result_phase が confirmed かつ complete が true "
            "ではない。集計に入れず、着順は作らない"
        ),
    )
    excluded: list[ExcludedRace] = Field(
        default_factory=list, description="取ったが集計に入れなかったレースと理由"
    )
    history_skipped: int = Field(
        0, description="履歴の取得段階で範囲外として落とした件数。IDは残っていない"
    )
    runner_omissions: list[RunnerOmission] = Field(
        default_factory=list, description="入れたレースのうち、表から外した頭数"
    )
    by_popularity: list[PopularityBandRow] = Field(default_factory=list)
    by_gate: list[GateRow] = Field(default_factory=list, description="馬番")
    by_frame: list[FrameRow] = Field(default_factory=list, description="枠番")
    running_style_definition: RunningStyleDefinition = Field(
        ..., description="脚質の定義。通過順の最初の数字と閾値"
    )
    by_running_style: list[RunningStyleRow] = Field(default_factory=list)
    by_jockey: list[JockeySurfaceRow] = Field(
        default_factory=list, description="騎手。芝とダートは別の行"
    )
    by_sire: list[SireSurfaceRow] = Field(
        default_factory=list, description="父。芝とダートは別の行"
    )
    stake_yen: int = Field(STAKE_YEN, description="1頭あたりの投資額（円）")
    roi_basis: str = Field(ROI_BASIS, description="ROI の定義")
    place_basis: str = Field(PLACE_BASIS, description="複勝の定義")
    source: str | None = Field(
        None, description="入れたレースの source を + でつないだもの。無ければ null"
    )
    fetched_at: datetime | None = Field(
        None,
        description="入れたレースの fetched_at が全て同じときだけその時刻。違うときは null",
    )
    fetched_at_min: datetime | None = Field(None, description="入れたレースの最も古い fetched_at")
    fetched_at_max: datetime | None = Field(None, description="入れたレースの最も新しい fetched_at")
    computed_at: datetime = Field(..., description="この集計を計算した時刻")
    warning: str | None = Field(None, description="集計全体の欠測。複数は ; でつなぐ")


def pending_races_warning(count: int) -> str | None:
    """Warning when races were left out because they are not final."""
    if count <= 0:
        return None
    if count == 1:
        return "1 race is not final and was left out"
    return f"{count} races are not final and were left out"


def running_style_definition() -> RunningStyleDefinition:
    """Four labels from the first passing call. The 4th-corner call is not read.

    Position 1 is always 逃げ. Every other horse is 先行, 差し, or 追込 at
    ``ceil(field_size / 3)`` and ``ceil(field_size * 2 / 3)``.
    """
    return RunningStyleDefinition(
        basis=STYLE_BASIS,
        position=(
            "4区分（逃げ・先行・差し・追込）。"
            "使うのは通過順の最初の数字。"
            "13-12-12-7 の初角は 13。最後の数字（4コーナー）は使わない"
        ),
        escape="最初の通過順位が 1 の馬は逃げ。この馬は三等分に入れない",
        front_end=(
            "1 以外の馬は ceil(field_size / 3) までが先行。この上限が 1 のときは先行は 0 頭"
        ),
        mid_end=("1 以外で ceil(field_size / 3) より後ろ、ceil(field_size * 2 / 3) までは差し"),
        closer="1 以外で ceil(field_size * 2 / 3) より後ろは追込",
        unusable=(
            "頭数はレースの field_size（ページの N頭）。"
            "通過順が読めない、頭数が無い、順位が頭数を超える走はラベルにしない。"
            "0頭や1頭の区分も sample_size 付きで残し、少ないからといって落とさない"
        ),
    )
