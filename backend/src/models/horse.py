"""Horse data models.

``Horse.performances`` は netkeiba の馬成績ページにある過去走。
今走の確定結果ではない。近走の能力評価にだけ使う。
"""

from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator

from backend.models.race import TRACK_BIAS_UNAVAILABLE, TrackBias, horse_track_bias
from backend.models.running_style import RunningStyle, derive_running_style
from backend.models.training import Training, training_not_loaded

PEDIGREE_SOURCE_PROFILE = "profile"
PEDIGREE_SOURCE_PED = "netkeiba_ped"
PEDIGREE_SOURCE_BOTH = "profile+netkeiba_ped"
PEDIGREE_FETCH_FAILED = "pedigree fetch failed"
PEDIGREE_FORBIDDEN = "pedigree returned 403"
PEDIGREE_LOGIN = "pedigree requires login"
PEDIGREE_TABLE_NOT_FOUND = "pedigree table not found"
PEDIGREE_TABLE_MALFORMED = "pedigree table malformed"
PEDIGREE_ABSENT = "pedigree not on profile or ped page"
PEDIGREE_INCOMPLETE = "pedigree incomplete"


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


class PedigreeHorse(BaseModel):
    """One ancestor taken from a netkeiba link. Missing sides are null, not guessed."""

    name: str | None = Field(None, description="馬名。ページに無いときは null")
    horse_id: str | None = Field(None, description="netkeiba の馬ID。リンクが無いときは null")


class Pedigree(BaseModel):
    """父・母・母父。

    プロフィール表に無いときは ``/horse/ped/{horse_id}/`` の ``blood_table``。
    レース名や兄弟馬のリンクからは埋めない。母父が表に無いときは ``damsire`` だけ null。
    """

    sire: PedigreeHorse | None = Field(None, description="父")
    dam: PedigreeHorse | None = Field(None, description="母")
    damsire: PedigreeHorse | None = Field(None, description="母父")
    source: str | None = Field(
        None,
        description="profile / netkeiba_ped / profile+netkeiba_ped。取れないときは null",
    )
    warning: str | None = Field(
        None,
        description=(
            "父と母が揃わない理由。"
            "pedigree fetch failed / pedigree returned 403 / pedigree requires login / "
            "pedigree table not found / pedigree table malformed / "
            "pedigree not on profile or ped page / pedigree incomplete"
        ),
    )


def absent_pedigree() -> Pedigree:
    return Pedigree(warning=PEDIGREE_ABSENT)


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
    pedigree: Pedigree | None = Field(
        None,
        description="父・母・母父。プロフィールに無いときは血統表。無い祖先は null",
    )
    running_style: RunningStyle | None = Field(
        None,
        description="近走の通過順から集計した脚質。成績を検証したあと必ず入る",
    )
    track_bias: TrackBias = Field(
        default_factory=horse_track_bias,
        description=(
            "当日のトラックバイアス。馬ペイロードには開催日が無いので"
            "競馬場・芝ダート・距離・馬場も null。"
            f"warning は {TRACK_BIAS_UNAVAILABLE}"
        ),
    )
    training: Training | None = Field(
        None,
        description=(
            "調教。speedkeiba の今週の重賞記事、競馬ブックの調教ページ、"
            "ログインできたときの調教履歴。ページに無い時計は null。"
            "オッズ・人気・レース名からは作らない"
        ),
    )
    warning: str | None = Field(
        None,
        description=(
            "成績を取れなかった理由。"
            "horse result fetch failed / horse result table not found / "
            "horse result table malformed / no performances on horse result page"
        ),
    )

    @model_validator(mode="after")
    def _fill_derived(self) -> "Horse":
        if self.pedigree is None:
            self.pedigree = absent_pedigree()
        if self.training is None:
            self.training = training_not_loaded()
        self.running_style = derive_running_style(self.performances)
        return self


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
    pedigree: Pedigree | None = Field(
        None,
        description="父・母・母父。出走馬一覧では /horse/ped/ だけを見る",
    )
    running_style: RunningStyle | None = Field(
        None,
        description="recent_performances の通過順から集計した脚質。この一覧は近走最大5走",
    )
    track_bias: TrackBias = Field(
        default_factory=horse_track_bias,
        description=(
            "このレースの馬場条件と、accessD / JRA 馬場情報にラベルがあるときの当日バイアス。"
            "ラベルが無いときは null と warning"
        ),
    )
    training: Training | None = Field(
        None,
        description=(
            "このレースの調教。競馬ブックの調教ページ、ログイン時の調教履歴、"
            "無ければ speedkeiba の重賞記事。どれにも無ければ null と warning"
        ),
    )

    @model_validator(mode="after")
    def _fill_derived(self) -> "HorseBasicInfo":
        if self.pedigree is None:
            self.pedigree = absent_pedigree()
        if self.training is None:
            self.training = training_not_loaded()
        self.running_style = derive_running_style(self.recent_performances)
        return self
