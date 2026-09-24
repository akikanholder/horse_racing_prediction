"""Horse data models."""

from datetime import date

from pydantic import BaseModel, Field


class HorsePerformance(BaseModel):
    """A single race performance record for a horse."""

    race_date: date = Field(..., description="レース日")
    race_id: str | None = Field(None, description="レースID")
    race_name: str = Field(..., description="レース名")
    venue: str = Field(..., description="開催場所")
    distance: int = Field(..., description="距離")
    track_type: str = Field(..., description="コース種別")
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
    prize_money: int | None = Field(None, description="賞金 (万円)")


class Horse(BaseModel):
    """Horse information."""

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
        default_factory=list, description="過去成績"
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
        default_factory=list, description="近走成績"
    )
