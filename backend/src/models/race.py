"""Race data models."""

from datetime import date, datetime

from pydantic import BaseModel, Field


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


class RaceCard(BaseModel):
    """Full race card with entries."""

    race: Race
    entries: list[RaceEntry] = Field(default_factory=list, description="出走馬リスト")


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

    bet_type: str = Field(..., description="馬券種。ページの表記（単勝、複勝、馬連、ワイド、3連複など）")
    combination: str = Field(..., description="馬番または枠番。ページの並び。馬単・3連単は着順どおり")
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
