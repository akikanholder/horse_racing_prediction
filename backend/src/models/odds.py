"""Odds quotes for EV sheets.

能力指標とは別物。期待値は RaceEntry.odds ではなく、この OddsQuote を読む。
単勝、馬連、三連複は odds（単一価格）。ワイドは odds_min と odds_max（範囲の片側から価格は作らない）。
複勝（place）は過去レースの確定オッズだけで使う。単一価格か、ページが範囲を印字したときの odds_min / odds_max。
"""

import re
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

OddsSource = Literal[
    "netkeiba_yoso",
    "netkeiba_live",
    "jradb",
    "jra_html",
    "jra_js",
    "netkeiba_html",
    "netkeiba_js",
    "headless",
]
OddsKind = Literal["yoso", "live"]
BetType = Literal["win", "wide", "umaren", "sanrenpuku", "place"]
OddsStatus = Literal["live", "yoso", "unavailable"]
OddsPrefer = Literal["live", "yoso"]
NonRunnerStatus = Literal["scratched", "excluded", "unknown"]
PageFetchSource = Literal["jra_html", "jra_js", "netkeiba_html", "netkeiba_js", "headless"]
# オッズAPIの JSON を読む文字コード。馬成績ページの EUC-JP ではない。
OddsEncoding = Literal["utf-8"]
# pre: API status の yoso / middle。post: API status の result（レース後の確定）。
RacePhase = Literal["pre", "post"]
ODDS_ENCODING: OddsEncoding = "utf-8"

_HORSE_NUMBER = re.compile(r"^(?:[1-9]|1[0-8])$")


class NonRunner(BaseModel):
    """A scratched or excluded horse on an odds page.

    馬番はページに印字されたものだけ。金額もオッズも作らない。
    """

    horse_number: int = Field(..., description="取消・除外の馬番", ge=1, le=18)
    status: NonRunnerStatus = Field(
        ...,
        description="scratched（取消・出走取消）/ excluded（除外・競走除外）/ unknown",
    )
    status_label: str | None = Field(
        None, description="ページに印字された区分。取消、出走取消、除外、競走除外など"
    )
    source: PageFetchSource | None = Field(
        None,
        description="この区分を得た方法。jra_html / jra_js / netkeiba_html / netkeiba_js / headless",
    )
    fetched_at: datetime | None = Field(None, description="この区分を得た時刻")


class OddsQuote(BaseModel):
    """One market or predicted-odds quote.

    source は実データの出所。CLI は呼び出し側のラベルであり、ここには入れない。
    kind=yoso は発売前のAI予想オッズ、kind=live は発売後の市場オッズ。
    prefer で kind は書き換えない。
    ページが ``----`` や ``取消`` と出している行は残し、価格は null、``withheld`` を true。
    価格は作らない。
    """

    race_id: str = Field(..., description="レースID")
    bet_type: BetType = Field(
        "win",
        description=(
            "馬券種。win（単勝）、place（複勝。過去の確定オッズだけ）、"
            "wide（ワイド）、umaren（馬連）、sanrenpuku（三連複）"
        ),
    )
    combination: str = Field(
        ...,
        description="単勝は馬番。ワイドと馬連は 3-7、三連複は 1-2-5 のように馬番の小さい順",
    )
    odds: float | None = Field(
        None,
        description="単勝・馬連・三連複のオッズ。複勝が単一価格のときもここ。ワイドでは空",
        gt=0,
    )
    odds_min: float | None = Field(
        None, description="ワイド、または複勝の範囲の下限。馬連・三連複では空", gt=0
    )
    odds_max: float | None = Field(
        None, description="ワイド、または複勝の範囲の上限。馬連・三連複では空", gt=0
    )
    source: OddsSource = Field(
        ...,
        description=(
            "取得元。発売中の引用は netkeiba_yoso / netkeiba_live。"
            "過去の確定オッズはページの jra_html / jra_js / netkeiba_html / netkeiba_js / headless もあり得る"
        ),
    )
    kind: OddsKind = Field(..., description="yoso=発売前の予想、live=発売後の市場")
    fetched_at: datetime = Field(..., description="取得日時")
    market_updated_at: datetime | None = Field(None, description="ページが示すオッズ更新日時")
    non_runner: bool = Field(
        False,
        description="この馬、またはこの組の馬が取消・除外のとき true。ページが組を出していないときは行を作らない",
    )
    withheld: bool = Field(
        False,
        description="ページが ---- または 取消 などで価格を出していないとき true。odds は null",
    )

    @model_validator(mode="after")
    def source_matches_kind(self) -> Self:
        """Keep predicted odds from being stored as a live market quote."""
        if self.kind == "yoso" and self.source != "netkeiba_yoso":
            raise ValueError("kind=yoso requires source=netkeiba_yoso")
        if self.kind == "live" and self.source == "netkeiba_yoso":
            raise ValueError("kind=live cannot use source=netkeiba_yoso")
        if self.bet_type == "win":
            self._check_win()
        elif self.bet_type == "place":
            self._check_place()
        elif self.bet_type == "wide":
            self._check_wide()
        elif self.bet_type == "umaren":
            self._check_umaren()
        else:
            self._check_sanrenpuku()
        return self

    def _check_place(self) -> None:
        """Place is a horse number. A printed range uses both ends. A single price stays in odds."""
        if _HORSE_NUMBER.fullmatch(self.combination) is None:
            raise ValueError("place combination must be a horse number")
        has_range = self.odds_min is not None or self.odds_max is not None
        if self.withheld:
            if self.odds is not None or has_range:
                raise ValueError("withheld place odds stay null")
            return
        if self.odds is not None and has_range:
            raise ValueError("place odds are either a single price or a range")
        if self.odds is not None:
            return
        if self.odds_min is None or self.odds_max is None:
            raise ValueError("place range requires odds_min and odds_max")
        if self.odds_min > self.odds_max:
            raise ValueError("odds_min cannot exceed odds_max")

    def _check_win(self) -> None:
        if _HORSE_NUMBER.fullmatch(self.combination) is None:
            raise ValueError("win combination must be a horse number")
        if self.odds_min is not None or self.odds_max is not None:
            raise ValueError("win odds are a single price")
        if self.withheld:
            if self.odds is not None:
                raise ValueError("withheld win odds stay null")
            return
        if self.odds is None:
            raise ValueError("win requires odds")

    def _check_wide(self) -> None:
        left, sep, right = self.combination.partition("-")
        if (
            sep != "-"
            or _HORSE_NUMBER.fullmatch(left) is None
            or _HORSE_NUMBER.fullmatch(right) is None
            or int(left) >= int(right)
        ):
            raise ValueError("wide combination must be lower-higher, such as 3-7")
        if self.odds is not None:
            raise ValueError("wide uses odds_min and odds_max")
        if self.withheld:
            if self.odds_min is not None or self.odds_max is not None:
                raise ValueError("withheld wide odds stay null")
            return
        if self.odds_min is None or self.odds_max is None:
            raise ValueError("wide requires odds_min and odds_max")
        if self.odds_min > self.odds_max:
            raise ValueError("odds_min cannot exceed odds_max")

    def _check_umaren(self) -> None:
        left, sep, right = self.combination.partition("-")
        if (
            sep != "-"
            or _HORSE_NUMBER.fullmatch(left) is None
            or _HORSE_NUMBER.fullmatch(right) is None
            or int(left) >= int(right)
        ):
            raise ValueError("umaren combination must be lower-higher, such as 3-7")
        if self.odds_min is not None or self.odds_max is not None:
            raise ValueError("umaren odds are a single price")
        if self.withheld:
            if self.odds is not None:
                raise ValueError("withheld umaren odds stay null")
            return
        if self.odds is None:
            raise ValueError("umaren requires odds")

    def _check_sanrenpuku(self) -> None:
        parts = self.combination.split("-")
        if len(parts) != 3 or any(_HORSE_NUMBER.fullmatch(part) is None for part in parts):
            raise ValueError("sanrenpuku combination must be three horse numbers, such as 1-2-5")
        numbers = [int(part) for part in parts]
        if numbers != sorted(numbers) or len(set(numbers)) != 3:
            raise ValueError("sanrenpuku combination must be ascending, such as 1-2-5")
        if self.odds_min is not None or self.odds_max is not None:
            raise ValueError("sanrenpuku odds are a single price")
        if self.withheld:
            if self.odds is not None:
                raise ValueError("withheld sanrenpuku odds stay null")
            return
        if self.odds is None:
            raise ValueError("sanrenpuku requires odds")


class RaceOdds(BaseModel):
    """Odds quotes for one race.

    status が unavailable のときは quotes は空。数値は作らない。
    prefer は呼び出し側の希望であり、kind の書き換えには使わない。
    prefer=live で市場オッズが取れないとき、yoso へは自動で切り替えない。
    予想オッズが必要なら呼び出し側が prefer=yoso を指定する。
    encoding は utf-8。race_phase は pre（レース前）か post（レース後）。
    失敗理由は reason。着順の warning とは別名にしない。
    """

    race_id: str = Field(..., description="レースID")
    bet_types: list[BetType] = Field(default_factory=list, description="返した馬券種")
    prefer: OddsPrefer = Field(
        "live",
        description="優先する種別。live が unavailable でも yoso へは切り替えない",
    )
    status: OddsStatus = Field(..., description="live / yoso / unavailable")
    reason: str | None = Field(
        None,
        description=(
            "取得できない理由。着順の warning に相当し、別名は置かない。"
            "空のときは quotes も空で、価格は作らない"
        ),
    )
    encoding: OddsEncoding = Field(
        ODDS_ENCODING,
        description=(
            "組み合わせとオッズ本文の文字コード。常に utf-8。"
            "combination は ASCII の馬番（1、3-7、1-2-5）。"
            "馬成績ページの EUC-JP では読まない"
        ),
    )
    race_phase: RacePhase | None = Field(
        None,
        description=(
            "pre=レース前（API の yoso と middle）。"
            "post=レース後の確定（API の result）。"
            "判別できない失敗は null。HTTP 400 や空の確定オッズから価格は作らない"
        ),
    )
    quotes: list[OddsQuote] = Field(default_factory=list, description="オッズ引用")
    non_runners: list[NonRunner] | None = Field(
        None,
        description=(
            "取消・除外の馬番。ページを読んで無いときは空。読めないときは null。"
            "馬番はページにあるものだけ"
        ),
    )
    warning: str | None = Field(
        None,
        description=(
            "取消・除外がページから読めない理由。価格の失敗は reason。"
            "JRA odds returned 403 / netkeiba odds returned 403 など"
        ),
    )
    fetched_at: datetime = Field(..., description="取得日時")
