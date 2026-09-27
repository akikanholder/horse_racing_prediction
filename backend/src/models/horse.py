"""Horse data models.

``Horse.performances`` は netkeiba の馬成績ページにある過去走。
今走の確定結果ではない。近走の能力評価にだけ使う。
"""

from datetime import date, datetime

from pydantic import BaseModel, Field


class HorsePerformance(BaseModel):
    """A single past-race performance from the horse result page.

    セルが無い、``**``、``取``、``計不`` のときは数値を作らず null。
    """

    race_date: date = Field(..., description="レース日")
    race_id: str | None = Field(None, description="レースID")
    race_name: str = Field(..., description="レース名")
    venue: str = Field(..., description="開催場所")
    distance: int | None = Field(None, description="距離")
    track_type: str | None = Field(None, description="コース種別 (芝/ダート/障害)")
    track_condition: str | None = Field(None, description="馬場状態")
    weather: str | None = Field(None, description="天候")
    entry_count: int | None = Field(None, description="出走頭数")
    horse_number: int | None = Field(None, description="馬番")
    rank: int | None = Field(None, description="着順")
    odds: float | None = Field(None, description="単勝オッズ")
    popularity: int | None = Field(None, description="人気")
    jockey_name: str | None = Field(None, description="騎手名")
    weight: float | None = Field(None, description="斤量")
    horse_weight: int | None = Field(None, description="馬体重")
    horse_weight_diff: int | None = Field(None, description="馬体重増減")
    finish_time: str | None = Field(None, description="タイム")
    margin: str | None = Field(None, description="着差")
    passing_order: str | None = Field(None, description="通過順位")
    last_3f: float | None = Field(None, description="上がり3F")
    prize_money: float | None = Field(None, description="賞金 (万円)")


class Horse(BaseModel):
    """競走馬情報。

    performances は ``/horse/result/{horse_id}/`` の過去走であり、
    今走のレース結果ではない。近走の能力評価にだけ使う。
    """

    horse_id: str = Field(..., description="馬ID")
    horse_name: str = Field(..., description="馬名")
    english_name: str | None = Field(None, description="英名")
    sex: str = Field(..., description="性別 (牡/牝/セ)")
    birth_date: date | None = Field(None, description="生年月日")
    birth_year: int | None = Field(None, description="生年")
    coat_color: str | None = Field(None, description="毛色")
    sire_name: str | None = Field(None, description="父名")
    sire_id: str | None = Field(None, description="父ID")
    dam_name: str | None = Field(None, description="母名")
    dam_id: str | None = Field(None, description="母ID")
    damsire_name: str | None = Field(None, description="母父名")
    damsire_id: str | None = Field(None, description="母父ID")
    breeder_name: str | None = Field(None, description="生産者")
    breeder_location: str | None = Field(None, description="産地")
    owner_name: str | None = Field(None, description="馬主名")
    trainer_name: str | None = Field(None, description="調教師名")
    trainer_id: str | None = Field(None, description="調教師ID")
    training_center: str | None = Field(None, description="所属 (美浦/栗東)")
    total_races: int | None = Field(None, description="総出走数")
    total_wins: int | None = Field(None, description="勝利数")
    total_earnings: int | None = Field(None, description="総獲得賞金 (万円)")
    performances: list[HorsePerformance] = Field(
        default_factory=list,
        description="過去成績。馬成績ページの近走履歴であり、今走の確定結果ではない",
    )
    fetched_at: datetime = Field(..., description="取得日時")
    warning: str | None = Field(
        None,
        description=(
            "成績を取れなかった理由。"
            "horse result fetch failed / horse result table not found / "
            "horse result table malformed / no performances on horse result page"
        ),
    )


class HorseBasicInfo(BaseModel):
    """Basic horse information for race cards."""

    horse_id: str = Field(..., description="馬ID")
    horse_name: str = Field(..., description="馬名")
    sex: str = Field(..., description="性別")
    age: int = Field(..., description="馬齢")
    sire_name: str | None = Field(None, description="父名")
    dam_name: str | None = Field(None, description="母名")
    trainer_name: str | None = Field(None, description="調教師名")
    training_center: str | None = Field(None, description="所属")
    recent_performances: list[HorsePerformance] = Field(
        default_factory=list,
        description="近走成績。馬成績ページの過去走であり、当該レースの確定結果ではない",
    )
