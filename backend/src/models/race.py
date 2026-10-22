"""Race data models."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.utils.date_utils import normalize_tokyo_clock

TRACK_BIAS_UNAVAILABLE = "day-of track bias unavailable without a race card"
TRACK_BIAS_NOT_LOADED = "day-of track bias not loaded from accessD/JRADB"
TrackBiasSource = Literal["accessd", "jra_baba", "accessd+jra_baba"]
TrackBiasSufficiency = Literal["unavailable", "thin", "sufficient"]
_GOING = frozenset({"良", "稍重", "重", "不良"})
_SURFACES = frozenset({"芝", "ダート", "障害"})


class TrackBias(BaseModel):
    """コース条件と、公式ページが印刷した当日バイアス。

    ``venue`` / ``surface`` / ``distance`` / ``going`` は、そのレースのページに
    書いてあったときだけ写す。``venue`` は開催ヘッダが race_id の競馬場と
    一致するときだけ。netkeiba ID の日次（2日目の ``02``）は場コードではない。
    ナビや 403 本文に出る場名は写さない。``inside_outside`` と ``pace_bias`` は
    accessD か JRA 馬場情報のバイアス見出しのセルだけ。外回り、使用コース、
    芝の傷み、含水率、クッション値、オッズ、人気、レース名からは作らない。
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


CardRunnerStatus = Literal["entered", "scratched", "excluded", "unknown"]
PageFetchSource = Literal["jra_html", "jra_js", "netkeiba_html", "netkeiba_js", "headless"]


class RaceEntry(BaseModel):
    """A single entry (horse) in a race.

    ``status`` は確定結果と同じ区分に、レース前の ``entered`` を足したもの。
    印字が ``取消`` / ``出走取消`` なら ``scratched``、``除外`` / ``競走除外`` なら
    ``excluded``。ページに無いときは null。着順の ``finished`` はレース前には使わない。
    """

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
    status: CardRunnerStatus | None = Field(
        None,
        description=(
            "entered（出走）/ scratched（取消・出走取消）/ "
            "excluded（除外・競走除外）/ unknown。ページに無いときは null"
        ),
    )
    status_label: str | None = Field(
        None,
        description="ページに印字された区分。取消、出走取消、除外、競走除外など。出走は null",
    )
    source: PageFetchSource | None = Field(
        None,
        description="この区分を得た方法。jra_html / jra_js / netkeiba_html / netkeiba_js / headless",
    )
    fetched_at: datetime | None = Field(None, description="この区分を得た時刻")


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
    post_time: str | None = Field(
        None, description="発走時刻。印字どおり Asia/Tokyo（15:30+09:00）。無ければ null"
    )
    post_time_source: str | None = Field(
        None,
        description="発走時刻を得た方法。jra_html / netkeiba_html / headless。無ければ null",
    )
    post_time_fetched_at: datetime | None = Field(None, description="発走時刻を得た時刻")
    venue: str = Field(..., description="開催場所")

    @field_validator("post_time", mode="before")
    @classmethod
    def _post_time_has_tokyo_offset(cls, value: object) -> object:
        """Store a printed clock with the Asia/Tokyo offset."""
        if isinstance(value, str):
            return normalize_tokyo_clock(value)
        return value
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
    warning: str | None = Field(
        None,
        description=(
            "取消・除外がページから読めない理由。"
            "読めている馬は status。読めない馬は status が null"
        ),
    )

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


ResultRunnerStatus = Literal[
    "finished",
    "scratched",
    "excluded",
    "did_not_finish",
    "disqualified",
    "unknown",
]
ResultPhase = Literal["provisional", "confirmed"]
ResultSource = Literal["netkeiba_html", "netkeiba_js", "headless"]
_NON_FINISH = frozenset({"scratched", "excluded", "did_not_finish", "disqualified"})


class SettledFinisher(BaseModel):
    """One runner on the settled race result page.

    着順が ``取消`` などのときは ``rank`` は null。空欄からタイムやオッズは作らない。
    ``status`` は固定コード、``status_label`` はページに印字された日本語のまま。
    """

    rank: int | None = Field(None, description="着順。取消・除外・中止・失格は null")
    status: ResultRunnerStatus | None = Field(
        None,
        description=(
            "finished / scratched（取消）/ excluded（除外）/ "
            "did_not_finish（中止・競走中止）/ disqualified（失格）/ unknown"
        ),
    )
    status_label: str | None = Field(
        None,
        description="ページに印字された区分。取消、除外、中止、競走中止、失格など。入着は null",
    )
    demoted: bool = Field(
        False, description="降着。ページに印字があるときだけ true。着順は公式の順位"
    )
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
    source: ResultSource | None = Field(
        None,
        description="この行を得た方法。netkeiba_html / netkeiba_js / headless",
    )
    fetched_at: datetime | None = Field(None, description="この行を得た時刻")

    @model_validator(mode="after")
    def non_finisher_rank_stays_null(self) -> "SettledFinisher":
        """取消・除外・中止・失格に着順を付けない。"""
        if self.status in _NON_FINISH and self.rank is not None:
            raise ValueError("non-finisher rank must be null")
        return self


class RaceRefund(BaseModel):
    """返還 printed on the result page or the official payout page.

    印字が無いレースでは要素を作らない。金額は作らない。
    """

    horse_numbers: list[int] = Field(
        default_factory=list, description="返還馬番。ページに印字された並び"
    )
    bet_types: list[str] | None = Field(None, description="印字された馬券種。書いていなければ null")
    bracket_numbers: list[int] | None = Field(None, description="返還同枠。書いていなければ null")
    source: str | None = Field(
        None,
        description="netkeiba_html / netkeiba_js / headless / jra_html。両方なら + でつなぐ",
    )
    fetched_at: datetime | None = Field(None, description="この返還を得た時刻")


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
    source: ResultSource | None = Field(
        None,
        description="この払戻を得た方法。netkeiba_html / netkeiba_js / headless",
    )
    fetched_at: datetime | None = Field(None, description="この払戻を得た時刻")


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
    race_class: str | None = Field(
        None, description="クラス。結果ページに印字されたときだけ。レース名からは作らない"
    )
    entry_count: int | None = Field(None, description="ページの N頭。印字が無ければ null")
    fetched_at: datetime = Field(
        ...,
        description="取得日時。呼び出し側が時刻を渡さないときは Asia/Tokyo（+09:00）",
    )
    http_status: int | None = Field(
        None,
        description=(
            "取得が HTTP 401 または 403 で止まったときのステータス。"
            "払戻金の「403円」など、警告の文字列からは付けない"
        ),
    )
    login_required: bool = Field(
        False,
        description="ログイン画面で止まったとき true。払戻の文面からは付けない",
    )
    source: str | None = Field(
        None,
        description=(
            "着順を得た方法。netkeiba_html / netkeiba_js / headless。"
            "混ざるときは netkeiba_html+netkeiba_js のように足す"
        ),
    )
    complete: bool = Field(
        False,
        description=(
            "走った頭数（取消・除外を除く）がページの N頭から取消・除外を引いた数と一致し、"
            "各馬に status があり、速報の上位表だけではないとき true"
        ),
    )
    result_phase: ResultPhase | None = Field(
        None,
        description="ページが示す provisional（確定前・暫定）か confirmed（確定）。印字が無ければ null",
    )
    results: list[SettledFinisher] = Field(
        default_factory=list,
        description="このレースの出走馬。結果ページの並び。未入着も含む。馬の近走成績ではない",
    )
    payouts: list[RacePayout] = Field(
        default_factory=list,
        description="結果ページの払戻。金額が無い馬券種は入れない",
    )
    refunds: list[RaceRefund] | None = Field(
        None,
        description=(
            "返還。払戻リストの隣。ページに印字が無いときは null。"
            "馬番と、印字されていれば馬券種。金額は作らない"
        ),
    )
    payout_note: str | None = Field(
        None,
        description="払戻が無い、または対応が取れない馬券種があるときの注記。金額は作らない",
    )
    warning: str | None = Field(
        None,
        description=(
            "確定結果を取れなかった、または頭数・区分・確定印・公式払戻が揃わない理由。"
            "race result fetch failed / race result not found / "
            "race not yet settled / race result table malformed / "
            "no finishers on race result page / race result returned 403 / "
            "race result requires login / race result incomplete / "
            "result phase was not on the page / JRA official payout was not reachable"
        ),
    )


class RaceSchedule(BaseModel):
    """Weekly race schedule."""

    week_start: date = Field(..., description="週の開始日")
    week_end: date = Field(..., description="週の終了日")
    races: list[Race] = Field(default_factory=list, description="レース一覧")
    fetched_at: datetime = Field(default_factory=datetime.now, description="取得日時")
