"""Shared data models passed between the buyer agent, sellers and the report."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Holding(BaseModel):
    asset: str
    free: float
    locked: float = 0.0
    price_usdc: float
    value_usdc: float
    change_24h_pct: float = 0.0

    @property
    def qty(self) -> float:
        return self.free + self.locked


class Portfolio(BaseModel):
    sub_account: str
    holdings: list[Holding]
    cash_usdc: float  # spendable USDC in the Agentic sub-account (the x402 budget)
    fetched_at: str = Field(default_factory=_now)

    @property
    def total_value_usdc(self) -> float:
        return round(sum(h.value_usdc for h in self.holdings) + self.cash_usdc, 2)

    def weight(self, asset: str) -> float:
        total = self.total_value_usdc or 1.0
        for h in self.holdings:
            if h.asset == asset:
                return round(100 * h.value_usdc / total, 2)
        if asset in ("USDC", "USDT"):
            return round(100 * self.cash_usdc / total, 2)
        return 0.0


class MarketQuote(BaseModel):
    symbol: str
    price: float
    change_24h_pct: float
    volume_24h_usdc: float
    funding_rate_8h_pct: float | None = None  # perp funding, if available


class Signal(BaseModel):
    """One structured finding returned by a seller agent."""

    kind: Literal["opportunity", "risk", "info"]
    asset: str
    title: str
    detail: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.6)
    metrics: dict[str, float] = Field(default_factory=dict)


class SellerResponse(BaseModel):
    seller: str
    generated_at: str = Field(default_factory=_now)
    signals: list[Signal]
    methodology: str = ""


class Payment(BaseModel):
    seller: str
    resource: str
    amount_usdc: float
    network: str
    tx_hash: str
    settled: bool
    mode: Literal["mock", "live"]
    at: str = Field(default_factory=_now)


class RebalanceAction(BaseModel):
    action: Literal["convert"]
    from_asset: str
    to_asset: str
    from_qty: float
    est_to_qty: float
    rationale: str


class Report(BaseModel):
    generated_at: str = Field(default_factory=_now)
    sub_account: str
    portfolio: Portfolio
    market: list[MarketQuote]
    plan_rationale: str
    payments: list[Payment]
    signals: list[Signal]
    narrative: str
    rebalance: list[RebalanceAction]
    spend_usdc: float
    budget_usdc: float
