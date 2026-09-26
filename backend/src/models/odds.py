"""Odds quotes for EV sheets.

能力指標とは別物。期待値は RaceEntry.odds ではなく、この OddsQuote を読む。
"""

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

OddsSource = Literal["netkeiba_yoso", "netkeiba_live", "jradb"]
OddsKind = Literal["yoso", "live"]
BetType = Literal["win"]
OddsStatus = Literal["live", "yoso", "unavailable"]
OddsPrefer = Literal["live", "yoso"]


class OddsQuote(BaseModel):
    """One market or predicted-odds quote.

    source は実データの出所。CLI は呼び出し側のラベルであり、ここには入れない。
    kind=yoso は発売前のAI予想オッズ、kind=live は発売後の市場オッズ。
    """

    race_id: str = Field(..., description="レースID")
    bet_type: BetType = Field("win", description="馬券種。現在は単勝のみ")
    combination: str = Field(..., description="単勝の組み合わせ（馬番）")
    odds: float = Field(..., description="オッズ", gt=0)
    source: OddsSource = Field(..., description="取得元")
    kind: OddsKind = Field(..., description="yoso=発売前の予想、live=発売後の市場")
    fetched_at: datetime = Field(..., description="取得日時")
    market_updated_at: datetime | None = Field(
        None, description="ページが示すオッズ更新日時"
    )

    @model_validator(mode="after")
    def source_matches_kind(self) -> Self:
        """Keep predicted odds from being stored as a live market quote."""
        if self.kind == "yoso" and self.source != "netkeiba_yoso":
            raise ValueError("kind=yoso requires source=netkeiba_yoso")
        if self.kind == "live" and self.source == "netkeiba_yoso":
            raise ValueError("kind=live cannot use source=netkeiba_yoso")
        if not self.combination.strip():
            raise ValueError("combination is required")
        return self


class RaceOdds(BaseModel):
    """Win odds for one race.

    status が unavailable のときは quotes は空。数値は作らない。
    prefer は呼び出し側の希望であり、kind の書き換えには使わない。
    """

    race_id: str = Field(..., description="レースID")
    bet_types: list[BetType] = Field(default_factory=list, description="返した馬券種")
    prefer: OddsPrefer = Field("live", description="優先する種別")
    status: OddsStatus = Field(..., description="live / yoso / unavailable")
    reason: str | None = Field(None, description="取得できない理由")
    quotes: list[OddsQuote] = Field(default_factory=list, description="オッズ引用")
    fetched_at: datetime = Field(..., description="取得日時")
