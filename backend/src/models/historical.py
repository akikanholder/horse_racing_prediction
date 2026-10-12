"""Past JRA flat races for EV backtests.

着順・払戻・返還は確定結果のモデルをそのまま載せる。
確定オッズは OddsQuote（withheld / non_runner）と NonRunner を使う。
発売中のオッズからは確定価格を作らない。
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from backend.models.odds import NonRunner, OddsQuote
from backend.models.race import SettledRaceResult

HISTORICAL_START = date(2025, 1, 1)
PRIORITY_VENUES = ("東京", "京都", "中山", "阪神")
HISTORICAL_REQUEST_DELAY = 1.5
FlatSurface = Literal["芝", "ダート"]


class HistoricalRace(BaseModel):
    """One past race: conditions, full finish, payouts, and final odds when confirmed."""

    race_id: str = Field(..., description="レースID")
    surface: FlatSurface | None = Field(
        None, description="芝またはダート。障害、またはページに無いときは null"
    )
    distance: int | None = Field(None, description="距離 (m)。ページに無いときは null")
    race_class: str | None = Field(None, description="クラス。印字が無ければ null")
    field_size: int | None = Field(None, description="頭数。ページの N頭。無ければ null")
    going: str | None = Field(None, description="馬場状態（良/稍重/重/不良）。無ければ null")
    flat: bool | None = Field(
        None, description="平地（芝・ダート）のとき true。障害のとき false。判別できないときは null"
    )
    in_scope: bool = Field(
        False,
        description="2025年以降の平地で、指定した競馬場に入るとき true",
    )
    result: SettledRaceResult = Field(
        ...,
        description="確定着順・払戻・返還。status / status_label / complete / result_phase は結果パーサのまま",
    )
    final_odds: list[OddsQuote] | None = Field(
        None,
        description=(
            "確定オッズ。ページが確定オッズと出すか、レース後のページのときだけ。"
            "発売中のオッズからは作らない。無いときは null"
        ),
    )
    non_runners: list[NonRunner] | None = Field(
        None,
        description="取消・除外。ページを読んで無いときは空。結果ページが読めないときは null",
    )
    odds_final: bool = Field(
        False,
        description="final_odds を確定として載せたときだけ true。発売中の数字では true にしない",
    )
    source: str | None = Field(
        None,
        description="着順と確定オッズを得た方法。混ざるときは + でつなぐ",
    )
    fetched_at: datetime = Field(
        ..., description="このレコードを組み立てた時刻。各値の fetched_at は別"
    )
    warning: str | None = Field(None, description="欠測や範囲外の理由。複数は ; でつなぐ")


class HistoricalBatch(BaseModel):
    """Races collected for one id or one date span."""

    races: list[HistoricalRace] = Field(
        default_factory=list, description="レース。日付指定では範囲内だけ"
    )
    skipped: int = Field(0, description="日付の列挙で範囲外として落とした件数")
    flat_only: bool = Field(True, description="障害を落とすとき true")
    venues: list[str] = Field(default_factory=list, description="指定した競馬場。空は全場")
    date_from: date | None = Field(None, description="取得した最初の日。race_id だけのときは null")
    date_to: date | None = Field(None, description="取得した最後の日。race_id だけのときは null")
    fetched_at: datetime = Field(..., description="この一覧を組み立てた時刻")
    warning: str | None = Field(None, description="日付が 2025 より前、または取りこぼしの理由")


class HistoricalExport(BaseModel):
    """Files written for surface calibration."""

    format: Literal["jsonl", "csv"] = Field(..., description="jsonl または csv")
    directory: str = Field(..., description="出力ディレクトリ")
    paths: list[str] = Field(default_factory=list, description="書いたファイル")
    race_count: int = Field(..., description="書いたレース数。範囲外は入れない")
    row_count: int = Field(..., description="走った行数。jsonl はレース数、csv は馬の行数")
    surfaces: list[str] = Field(default_factory=list, description="ファイルに入った芝・ダート")
