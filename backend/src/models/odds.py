"""Odds quotes for EV sheets.

能力指標とは別物。期待値は RaceEntry.odds ではなく、この OddsQuote を読む。
単勝、馬連、三連複は odds（単一価格）。ワイドは odds_min と odds_max（範囲の片側から価格は作らない）。
"""

import re
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

OddsSource = Literal["netkeiba_yoso", "netkeiba_live", "jradb"]
OddsKind = Literal["yoso", "live"]
BetType = Literal["win", "wide", "umaren", "sanrenpuku"]
OddsStatus = Literal["live", "yoso", "unavailable"]
OddsPrefer = Literal["live", "yoso"]

_HORSE_NUMBER = re.compile(r"^(?:[1-9]|1[0-8])$")


class OddsQuote(BaseModel):
    """One market or predicted-odds quote.

    source は実データの出所。CLI は呼び出し側のラベルであり、ここには入れない。
    kind=yoso は発売前のAI予想オッズ、kind=live は発売後の市場オッズ。
    prefer で kind は書き換えない。
    """

    race_id: str = Field(..., description="レースID")
    bet_type: BetType = Field(
        "win",
        description="馬券種。win（単勝）、wide（ワイド）、umaren（馬連）、sanrenpuku（三連複）",
    )
    combination: str = Field(
        ...,
        description="単勝は馬番。ワイドと馬連は 3-7、三連複は 1-2-5 のように馬番の小さい順",
    )
    odds: float | None = Field(None, description="単勝・馬連・三連複のオッズ。ワイドでは空", gt=0)
    odds_min: float | None = Field(None, description="ワイドの下限オッズ。馬連・三連複では空", gt=0)
    odds_max: float | None = Field(None, description="ワイドの上限オッズ。馬連・三連複では空", gt=0)
    source: OddsSource = Field(..., description="取得元")
    kind: OddsKind = Field(..., description="yoso=発売前の予想、live=発売後の市場")
    fetched_at: datetime = Field(..., description="取得日時")
    market_updated_at: datetime | None = Field(None, description="ページが示すオッズ更新日時")

    @model_validator(mode="after")
    def source_matches_kind(self) -> Self:
        """Keep predicted odds from being stored as a live market quote."""
        if self.kind == "yoso" and self.source != "netkeiba_yoso":
            raise ValueError("kind=yoso requires source=netkeiba_yoso")
        if self.kind == "live" and self.source == "netkeiba_yoso":
            raise ValueError("kind=live cannot use source=netkeiba_yoso")
        if self.bet_type == "win":
            self._check_win()
        elif self.bet_type == "wide":
            self._check_wide()
        elif self.bet_type == "umaren":
            self._check_umaren()
        else:
            self._check_sanrenpuku()
        return self

    def _check_win(self) -> None:
        if _HORSE_NUMBER.fullmatch(self.combination) is None:
            raise ValueError("win combination must be a horse number")
        if self.odds is None:
            raise ValueError("win requires odds")
        if self.odds_min is not None or self.odds_max is not None:
            raise ValueError("win odds are a single price")

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
        if self.odds is None:
            raise ValueError("umaren requires odds")
        if self.odds_min is not None or self.odds_max is not None:
            raise ValueError("umaren odds are a single price")

    def _check_sanrenpuku(self) -> None:
        parts = self.combination.split("-")
        if len(parts) != 3 or any(_HORSE_NUMBER.fullmatch(part) is None for part in parts):
            raise ValueError("sanrenpuku combination must be three horse numbers, such as 1-2-5")
        numbers = [int(part) for part in parts]
        if numbers != sorted(numbers) or len(set(numbers)) != 3:
            raise ValueError("sanrenpuku combination must be ascending, such as 1-2-5")
        if self.odds is None:
            raise ValueError("sanrenpuku requires odds")
        if self.odds_min is not None or self.odds_max is not None:
            raise ValueError("sanrenpuku odds are a single price")


class RaceOdds(BaseModel):
    """Odds quotes for one race.

    status が unavailable のときは quotes は空。数値は作らない。
    prefer は呼び出し側の希望であり、kind の書き換えには使わない。
    prefer=live で市場オッズが取れないとき、yoso へは自動で切り替えない。
    予想オッズが必要なら呼び出し側が prefer=yoso を指定する。
    """

    race_id: str = Field(..., description="レースID")
    bet_types: list[BetType] = Field(default_factory=list, description="返した馬券種")
    prefer: OddsPrefer = Field(
        "live",
        description="優先する種別。live が unavailable でも yoso へは切り替えない",
    )
    status: OddsStatus = Field(..., description="live / yoso / unavailable")
    reason: str | None = Field(None, description="取得できない理由")
    quotes: list[OddsQuote] = Field(default_factory=list, description="オッズ引用")
    fetched_at: datetime = Field(..., description="取得日時")
