"""Shared data models passed between the buyer agent, sellers and the report."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DataProvenance(BaseModel):
    """Where a piece of data came from, and how fresh / trustworthy it is."""

    label: str  # e.g. "market", "snapshot", "seller:risk"
    source: str  # "binance-public-rest" | "synthetic-fallback" | "agent-os-mcp-snapshot" | ...
    as_of: str = Field(default_factory=_now)  # when THIS read happened
    captured_at: str | None = None  # original capture time (snapshots) — never the read time
    degraded: bool = False  # a fallback / synthetic path was taken
    stale: bool = False  # older than the configured freshness window
    reason: str = ""  # why degraded/stale, if so


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
    # provenance carried with the finding (best-effort; older sellers omit it)
    source: str = ""
    method: str = ""
    as_of: str = ""
    # traceability: which seller produced it, the payment that bought it, the
    # evidence it rests on, and how long it is meant to be actionable
    seller_id: str = ""
    request_id: str = ""  # -> Payment.request_id, links a signal to what paid for it
    evidence: dict = Field(default_factory=dict)
    horizon: str = ""  # e.g. "intraday", "7d", "swing"
    valid_until: str = ""  # ISO-8601; empty == unspecified


class SellerResponse(BaseModel):
    seller: str
    generated_at: str = Field(default_factory=_now)
    signals: list[Signal]
    methodology: str = ""
    provenance: dict = Field(default_factory=dict)


class Payment(BaseModel):
    seller: str
    resource: str
    amount_usdc: float
    network: str
    tx_hash: str
    settled: bool  # kept for template compat: True iff pay_status == "settled"
    mode: Literal["mock", "live"]
    at: str = Field(default_factory=_now)
    request_id: str = ""
    # settled | settlement_unknown | failed | reserved
    pay_status: str = "settled"
    # delivered | pending | failed
    delivery_status: str = "delivered"
    authorization: str = ""  # the EIP-3009 nonce (audit trail; not the signature)


class RebalanceAction(BaseModel):
    action: Literal["convert"] = "convert"
    from_asset: str
    to_asset: str
    from_qty: float
    est_to_qty: float
    rationale: str
    status: str = "proposed"  # proposed | validated | rejected | quoted | executed

    @field_validator("from_asset", "to_asset")
    @classmethod
    def _upper(cls, v: str) -> str:
        return str(v).strip().upper()

    @field_validator("from_qty")
    @classmethod
    def _from_qty_positive_finite(cls, v: float) -> float:
        v = float(v)
        if not math.isfinite(v) or v <= 0:
            raise ValueError(f"from_qty must be a positive finite number, got {v!r}")
        return v

    @field_validator("est_to_qty")
    @classmethod
    def _est_finite(cls, v: float) -> float:
        v = float(v)
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"est_to_qty must be a non-negative finite number, got {v!r}")
        return v

    @field_validator("to_asset")
    @classmethod
    def _distinct(cls, v: str, info) -> str:
        if info.data.get("from_asset") == v:
            raise ValueError("from_asset and to_asset must differ")
        return v


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

    # --- reliability / trust surface (new; all have safe defaults) ---
    run_id: str = ""
    binance_mode: str = "mock"
    x402_mode: str = "mock"
    budget_remaining_usdc: float = 0.0
    # data trust
    provenance: list[DataProvenance] = Field(default_factory=list)
    data_degraded: bool = False
    data_stale: bool = False
    # analysis completeness
    sellers_attempted: int = 0
    sellers_delivered: int = 0
    seller_errors: list[str] = Field(default_factory=list)
    analysis_complete: bool = True
    llm_status: str = "rules"  # "llm" | "rules" | "rules-fallback: <err>"
    unresolved_payments: list[dict] = Field(default_factory=list)
    # execution gate
    trade_execution_allowed: bool = True
    trade_block_reasons: list[str] = Field(default_factory=list)

    @property
    def freshness_note(self) -> str:
        if self.data_stale:
            return "STALE — data older than the freshness window; live execution blocked"
        if self.data_degraded:
            return "DEGRADED — some inputs came from a synthetic fallback"
        return "OK"
