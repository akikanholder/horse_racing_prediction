"""Pre-race training (調教) taken from a page, never from odds or the race name."""

from datetime import date as date_type

from pydantic import BaseModel, Field

SOURCE_SPEEDKEIBA = "speedkeiba"
SOURCE_KEIBABOOK_SMART = "keibabook_smart"

TRAINING_FETCH_FAILED = "training fetch failed"
TRAINING_TABLE_NOT_FOUND = "training table not found"
TRAINING_TABLE_MALFORMED = "training table malformed"
TRAINING_NOT_ON_KEIBABOOK = "training not on keibabook page without login"
TRAINING_NOT_ON_EITHER = (
    "training not on keibabook page without login, and not on the speedkeiba article"
)
TRAINING_HORSE_UNAVAILABLE = (
    "training unavailable: not on speedkeiba weekly index; "
    "keibabook training history requires login"
)
TRAINING_AMBIGUOUS = "training ambiguous on speedkeiba"
TRAINING_THIN = "training sample thin"
TRAINING_NOT_LOADED = "training not loaded"

_HARD_WARNINGS = frozenset(
    {
        TRAINING_FETCH_FAILED,
        TRAINING_TABLE_NOT_FOUND,
        TRAINING_TABLE_MALFORMED,
        TRAINING_NOT_ON_KEIBABOOK,
        TRAINING_NOT_ON_EITHER,
        TRAINING_HORSE_UNAVAILABLE,
        TRAINING_AMBIGUOUS,
        TRAINING_NOT_LOADED,
    }
)


class TrainingSplit(BaseModel):
    """One labeled clock. The label is the column header on that page."""

    label: str = Field(..., description="ページの列名。5F、半哩、4F（坂路の列）など")
    time: str = Field(..., description="そのセルの時計。ページの文字のまま")


class PublishedAverage(BaseModel):
    """A comparison row the page itself prints, such as speedkeiba's `(平均)`."""

    label: str = Field(..., description="ページのラベル。(平均) など")
    course: str | None = Field(None, description="同じ行に書いてあるコース")
    time: str | None = Field(None, description="ページの平均時計。こちらでは計算しない")


class TrainingLap(BaseModel):
    """A labeled lap row. 好調 / 凡走 are not copied; those rows use popularity."""

    label: str = Field(..., description="行ラベル。今走")
    phase: str | None = Field(None, description="一週前 / 本追切。キャプションにあるときだけ")
    course: str | None = Field(None, description="キャプションのコース。坂路など")
    splits: list[TrainingSplit] = Field(default_factory=list, description="ハロンごとのラップ")


class TrainingSummary(BaseModel):
    """Aggregates printed on the page. Nothing here is computed by us."""

    comment: str | None = Field(None, description="追い切り短評。ページの文")
    arrow: str | None = Field(None, description="矢印。→ などをそのまま。評価点にはしない")
    evaluation: str | None = Field(None, description="speedkeiba が印字した評価文")
    average: PublishedAverage | None = Field(None, description="ページにある平均行")
    laps: list[TrainingLap] = Field(default_factory=list, description="今走のラップ。好調・凡走は入れない")


class Workout(BaseModel):
    """One workout row from the source page."""

    date: date_type | None = Field(None, description="年月日。年がページから決まらないときは null")
    date_label: str | None = Field(None, description="日付セルの文字。■ や 9/23 (水) をそのまま")
    course: str | None = Field(None, description="コース。美Ｗ、美浦 W、栗東 坂 など")
    distance: str | None = Field(
        None, description="時計が入っている一番長い列の名前。列名が無い行は null"
    )
    time: str | None = Field(
        None, description="distance の時計、または列名の無い行の時計文字列"
    )
    condition: str | None = Field(None, description="馬場。良、稍重、不。ページの略記のまま")
    rank_in_group: int | None = Field(None, description="回り位置。［８］なら 8。空なら null")
    rider: str | None = Field(None, description="騎乗者")
    gait: str | None = Field(None, description="脚色")
    comment: str | None = Field(None, description="その行の短評")
    partner_note: str | None = Field(None, description="併せ馬の行。時計には分解しない")
    mark: str | None = Field(None, description="☆ など。空なら null")
    final_workout: bool | None = Field(
        None, description="本追切の行（☆ または oikiri）のときだけ true。それ以外は null"
    )
    long_gap: bool | None = Field(
        None, description="日付に * があるとき true。記号だけの日付は null"
    )
    slope_reps: int | None = Field(None, description="坂路の回数。1回 のとき 1。無いときは null")
    section: str | None = Field(None, description="今回 / 前回連対時。speedkeiba の見出し")
    splits: list[TrainingSplit] = Field(default_factory=list, description="列名つきの時計")


class Training(BaseModel):
    """Recent workouts for one horse.

    Missing clocks stay null. Odds, popularity, and the race title are not copied in.
    """

    workouts: list[Workout] = Field(default_factory=list, description="ページ順の調教")
    summary: TrainingSummary | None = Field(
        None, description="ページが載せている短評・矢印・平均・今走ラップ"
    )
    source: str | None = Field(
        None, description="speedkeiba / keibabook_smart。取れないときは null"
    )
    data_sufficiency: str = Field(
        ...,
        description="sufficient / thin / unavailable。日付つきの時計が2本未満は thin",
    )
    warning: str | None = Field(None, description="薄い、壊れている、ログインが要る、などの理由")


def unavailable_training(warning: str) -> Training:
    """No workouts. The warning says why, including missing login when that is the reason."""
    return Training(
        workouts=[],
        summary=None,
        source=None,
        data_sufficiency="unavailable",
        warning=warning,
    )


def training_not_loaded() -> Training:
    """Placeholder until RaceService attaches a real fetch."""
    return unavailable_training(TRAINING_NOT_LOADED)


def _summary_present(summary: TrainingSummary | None) -> bool:
    if summary is None:
        return False
    return bool(
        summary.comment or summary.arrow or summary.evaluation or summary.average or summary.laps
    )


def build_training(
    *,
    workouts: list[Workout],
    summary: TrainingSummary | None,
    source: str | None,
    warning: str | None,
) -> Training:
    """Set sufficiency from the rows we actually kept. Hard failures stay unavailable."""
    if summary is not None and not _summary_present(summary):
        summary = None
    if warning in _HARD_WARNINGS:
        kept_summary = summary if _summary_present(summary) else None
        return Training(
            workouts=workouts,
            summary=kept_summary,
            source=source if kept_summary is not None else None,
            data_sufficiency="unavailable",
            warning=warning,
        )
    dated = [row for row in workouts if row.date and row.time and row.course]
    if len(dated) >= 2:
        return Training(
            workouts=workouts,
            summary=summary,
            source=source,
            data_sufficiency="sufficient",
            warning=None,
        )
    if any(row.time for row in workouts) or summary is not None:
        return Training(
            workouts=workouts,
            summary=summary,
            source=source,
            data_sufficiency="thin",
            warning=TRAINING_THIN,
        )
    return unavailable_training(warning or TRAINING_NOT_ON_KEIBABOOK)
