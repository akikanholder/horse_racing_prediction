"""Race data models."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

TRACK_BIAS_UNAVAILABLE = "day-of track bias unavailable without a race card"
TRACK_BIAS_NOT_LOADED = "day-of track bias not loaded from accessD/JRADB"
TrackBiasSource = Literal["accessd", "jra_baba", "accessd+jra_baba"]
TrackBiasSufficiency = Literal["unavailable", "thin", "sufficient"]
_GOING = frozenset({"良", "稍重", "重", "不良"})
_SURFACES = frozenset({"芝", "ダート", "障害"})


class TrackBias(BaseModel):
    """コース条件と、公式ページが印刷した当日バイアス。

    ``venue`` / ``surface`` / ``distance`` / ``going`` は、そのレースのページに
    書いてあったときだけ写す。``inside_outside`` と ``pace_bias`` は accessD か
    JRA 馬場情報のバイアス見出しのセルだけ。外回り、使用コース、芝の傷み、
    含水率、クッション値、オッズ、人気、レース名からは作らない。
    """

    venue: str | None = Field(None, description="競馬場。ページに無い、または不明のときは null")
    surface: str | None = Field(None, description="芝 / ダート / 障害。不明のときは null")
    distance: int | None = Field(None, description="距離 (m)。0 や欠測は null")
    going: str | None = Field(
        None, description="馬場状態（良/稍重/重/不良）。ページに無いときは null"
    )
    inside_outside: str | None = Field(
        None, description="内外バイアス。公式ページの見出しセルに無いときは null"
    )
    pace_bias: str | None = Field(
        None, description="先行有利などのバイアス。公式ページの見出しセルに無いときは null"
    )
    source: TrackBiasSource | None = Field(
        None,
        description="accessd / jra_baba / accessd+jra_baba。ラベルが無いときは null",
    )
    data_sufficiency: TrackBiasSufficiency = Field(
        "unavailable",
        description="sufficient は内外とペースの両方。thin は片方。無いときは unavailable",
    )
    warning: str | None = Field(
        None,
        description=(
            "欠測の理由。馬単体は without a race card。"
            "未取得は not loaded from accessD/JRADB。"
            "ページを見てラベルが無いときは not published"
        ),
    )


def horse_track_bias() -> TrackBias:
    """Horse payload has no race day. Conditions and bias stay null."""
    return TrackBias(
        venue=None,
        surface=None,
        distance=None,
        going=None,
        inside_outside=None,
        pace_bias=None,
        source=None,
        data_sufficiency="unavailable",
        warning=TRACK_BIAS_UNAVAILABLE,
    )


def track_bias_from_race(race: "Race") -> TrackBias:
    """Copy conditions the race page actually had. Bias labels are not loaded yet."""
    venue = race.venue if race.venue and race.venue != "不明" else None
    surface = race.track_type if race.track_type in _SURFACES else None
    distance = race.distance if race.distance > 0 else None
    going = race.track_condition if race.track_condition in _GOING else None
    return TrackBias(
        venue=venue,
        surface=surface,
        distance=distance,
        going=going,
        inside_outside=None,
        pace_bias=None,
        source=None,
        data_sufficiency="unavailable",
        warning=TRACK_BIAS_NOT_LOADED,
    )


def _publication_applied(bias: TrackBias) -> bool:
    """True after a lookup replaced the not-loaded placeholder."""
    return bias.warning != TRACK_BIAS_NOT_LOADED


class RaceEntry(BaseModel):
    """A single entry (horse) in a race."""

    bracket_number: int = Field(..., description="枠番", ge=1, le=8)
    horse_number: int = Field(..., description="馬番", ge=1)
    horse_name: str = Field(..., description="馬名")
    horse_id: str | None = Field(None, description="馬ID")
    sex: str | None = Field(None, description="性別 (牡/牝/セ)")
    age: int | None = Field(None, description="馬齢")
    weight: float | None = Field(None, description="斤量")
    jockey_name: str | None = Field(None, description="騎手名")
    jockey_id: str | None = Field(None, description="騎手ID")
    trainer_name: str | None = Field(None, description="調教師名")
    trainer_id: str | None = Field(None, description="調教師ID")
    owner_name: str | None = Field(None, description="馬主名")
    horse_weight: int | None = Field(None, description="馬体重")
    horse_weight_diff: int | None = Field(None, description="馬体重増減")
    odds: float | None = Field(None, description="単勝オッズ")
    popularity: int | None = Field(None, description="人気順")


class RaceResult(BaseModel):
    """Race result for a single horse."""

    rank: int | None = Field(None, description="着順 (None if DNF)")
    bracket_number: int = Field(..., description="枠番")
    horse_number: int = Field(..., description="馬番")
    horse_name: str = Field(..., description="馬名")
    horse_id: str | None = Field(None, description="馬ID")
    finish_time: str | None = Field(None, description="タイム")
    margin: str | None = Field(None, description="着差")
    passing_order: str | None = Field(None, description="通過順位")
    last_3f: float | None = Field(None, description="上がり3F")
    jockey_name: str | None = Field(None, description="騎手名")
    odds: float | None = Field(None, description="単勝オッズ")
    popularity: int | None = Field(None, description="人気")


class Race(BaseModel):
    """Basic race information."""

    race_id: str = Field(..., description="レースID")
    race_name: str = Field(..., description="レース名")
    race_number: int = Field(..., description="レース番号", ge=1, le=12)
    race_date: date = Field(..., description="開催日")
    post_time: str | None = Field(None, description="発走時刻")
    venue: str = Field(..., description="開催場所")
    venue_code: str | None = Field(None, description="競馬場コード")
    round_number: int | None = Field(None, description="開催回次")
    day_number: int | None = Field(None, description="開催日次")
    distance: int = Field(..., description="距離 (m)")
    track_type: str = Field(..., description="コース種別 (芝/ダート/障害)")
    direction: str | None = Field(None, description="回り (右/左/直線)")
    track_condition: str | None = Field(None, description="馬場状態 (良/稍重/重/不良)")
    weather: str | None = Field(None, description="天候")
    race_class: str | None = Field(None, description="クラス")
    race_grade: str | None = Field(None, description="グレード")
    age_condition: str | None = Field(None, description="年齢条件")
    sex_condition: str | None = Field(None, description="性別条件")
    weight_condition: str | None = Field(None, description="斤量条件")
    prize_money: int | None = Field(None, description="1着賞金 (万円)")
    entry_count: int | None = Field(None, description="出走頭数")
    track_bias: TrackBias | None = Field(
        None,
        description=(
            "当日バイアス。週次一覧では null。"
            "出馬表では競馬場・芝ダート・距離・馬場状態を写し、"
            "内外とペースは accessD か JRA 馬場情報にラベルがあるときだけ"
        ),
    )


class RaceCard(BaseModel):
    """Full race card with entries."""

    race: Race
    entries: list[RaceEntry] = Field(default_factory=list, description="出走馬リスト")

    @model_validator(mode="after")
    def _fill_track_bias(self) -> "RaceCard":
        base = track_bias_from_race(self.race)
        existing = self.race.track_bias
        if existing is not None and _publication_applied(existing):
            base = base.model_copy(
                update={
                    "inside_outside": existing.inside_outside,
                    "pace_bias": existing.pace_bias,
                    "source": existing.source,
                    "data_sufficiency": existing.data_sufficiency,
                    "warning": existing.warning,
                }
            )
        if self.race.track_bias != base:
            self.race = self.race.model_copy(update={"track_bias": base})
        return self


class RaceWithResults(BaseModel):
    """Race with results."""

    race: Race
    results: list[RaceResult] = Field(default_factory=list, description="レース結果")


class SettledFinisher(BaseModel):
    """One row on the settled race result page.

    着順が ``取消`` などのときは ``rank`` は null。空欄からタイムやオッズは作らない。
    """

    rank: int | None = Field(None, description="着順。取消・除外・中止・失格は null")
    bracket_number: int | None = Field(None, description="枠番")
    horse_number: int | None = Field(None, description="馬番")
    horse_name: str | None = Field(None, description="馬名")
    horse_id: str | None = Field(None, description="馬ID")
    weight: float | None = Field(None, description="斤量")
    jockey_name: str | None = Field(None, description="騎手名")
    finish_time: str | None = Field(None, description="タイム")
    margin: str | None = Field(None, description="着差")
    last_3f: float | None = Field(None, description="上がり3F（結果ページの後3F）")
    popularity: int | None = Field(None, description="単勝人気")
    odds: float | None = Field(None, description="単勝オッズ")
    passing_order: str | None = Field(None, description="コーナー通過順")


class RacePayout(BaseModel):
    """One ticket on the result page 払戻 table.

    ページに金額が無い馬券種は作らない。
    """

    bet_type: str = Field(
        ..., description="馬券種。ページの表記（単勝、複勝、馬連、ワイド、3連複など）"
    )
    combination: str = Field(
        ..., description="馬番または枠番。ページの並び。馬単・3連単は着順どおり"
    )
    payout: int = Field(..., description="払戻金（円）", gt=0)
    popularity: int | None = Field(None, description="人気。ページに無いときは null")


class SettledRaceResult(BaseModel):
    """確定した1レースの着順と払戻。

    ``cli horse`` の近走成績ではない。レース前の期待値の入力に混ぜない。
    取得できない、または未確定のときは着順と払戻は空で、``warning`` に理由が入る。
    """

    race_id: str = Field(..., description="レースID")
    race_name: str | None = Field(None, description="レース名")
    race_date: date | None = Field(None, description="開催日")
    venue: str | None = Field(None, description="競馬場")
    distance: int | None = Field(None, description="距離 (m)")
    track_type: str | None = Field(None, description="コース種別 (芝/ダート/障害)")
    track_condition: str | None = Field(None, description="馬場状態 (良/稍重/重/不良)")
    weather: str | None = Field(None, description="天候")
    fetched_at: datetime = Field(..., description="取得日時")
    results: list[SettledFinisher] = Field(
        default_factory=list,
        description="このレースの確定着順。結果ページの並び。馬の近走成績ではない",
    )
    payouts: list[RacePayout] = Field(
        default_factory=list,
        description="結果ページの払戻。金額が無い馬券種は入れない",
    )
    payout_note: str | None = Field(
        None,
        description="払戻が無い、または対応が取れない馬券種があるときの注記。金額は作らない",
    )
    warning: str | None = Field(
        None,
        description=(
            "確定結果を取れなかった理由。"
            "race result fetch failed / race result not found / "
            "race not yet settled / race result table malformed / "
            "no finishers on race result page"
        ),
    )


class RaceSchedule(BaseModel):
    """Weekly race schedule."""

    week_start: date = Field(..., description="週の開始日")
    week_end: date = Field(..., description="週の終了日")
    races: list[Race] = Field(default_factory=list, description="レース一覧")
    fetched_at: datetime = Field(default_factory=datetime.now, description="取得日時")
