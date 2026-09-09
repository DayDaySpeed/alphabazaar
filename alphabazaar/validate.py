"""Deterministic pre-trade validation — independent of the LLM.

The planner / synthesizer (rules or ``claude-opus-5``) *proposes* rebalance
actions. Nothing here trusts them to have followed the rules. Every proposed
``RebalanceAction`` is re-checked against the actual portfolio before it can be
quoted, approved or executed:

* amount is a positive, finite number
* the asset is actually held
* the amount does not exceed the **free** (unlocked) balance
* notional is within [min, max] limits

Precision / lot-size / exchange minimums require live ``exchangeInfo`` (Binance
MCP), which is unavailable in ``mock`` / ``snapshot`` — those checks are marked
``unverified`` rather than guessed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import settings
from .models import Portfolio, RebalanceAction

STABLES = {"USDC", "USDT", "BUSD", "FDUSD", "DAI", "TUSD"}


@dataclass
class ValidationResult:
    ok: bool
    action: RebalanceAction
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    notional_usdc: float = 0.0

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "errors": self.errors, "warnings": self.warnings,
            "unverified": self.unverified, "notional_usdc": round(self.notional_usdc, 2),
        }


def validate_rebalance(
    action: RebalanceAction,
    portfolio: Portfolio,
    *,
    min_notional_usdc: float | None = None,
    max_notional_usdc: float | None = None,
) -> ValidationResult:
    min_notional = settings.trade_min_notional_usdc if min_notional_usdc is None else min_notional_usdc
    max_notional = (
        max_notional_usdc
        if max_notional_usdc is not None
        else (settings.trade_max_notional_usdc or None)
    )

    errors: list[str] = []
    warnings: list[str] = []
    unverified = [
        "quantity step precision (needs convert_queryOrderQuantityPrecisionPerAsset)",
        "exchange minimum convert size (needs a live convert quote / pair limits)",
    ]

    qty = float(action.from_qty)
    if not math.isfinite(qty) or qty <= 0:
        errors.append(f"from_qty must be positive and finite (got {qty!r})")
    if not math.isfinite(float(action.est_to_qty)) or float(action.est_to_qty) < 0:
        errors.append(f"est_to_qty must be non-negative and finite (got {action.est_to_qty!r})")
    if action.from_asset == action.to_asset:
        errors.append("from_asset == to_asset")

    holding = next((h for h in portfolio.holdings if h.asset == action.from_asset), None)
    price = 0.0
    if holding is None:
        errors.append(f"{action.from_asset} is not held in this sub-account")
    else:
        price = holding.price_usdc
        if math.isfinite(qty) and qty > holding.free + 1e-9:
            errors.append(
                f"from_qty {qty:g} {action.from_asset} exceeds FREE balance "
                f"{holding.free:g} (locked {holding.locked:g} excluded)"
            )

    notional = qty * price if math.isfinite(qty) else 0.0
    if holding is not None and notional < min_notional:
        errors.append(f"notional ${notional:.2f} below minimum ${min_notional:.2f}")
    if max_notional and notional > max_notional:
        errors.append(f"notional ${notional:.2f} above per-trade cap ${max_notional:.2f}")

    if action.to_asset not in STABLES and not any(
        h.asset == action.to_asset for h in portfolio.holdings
    ):
        warnings.append(f"to_asset {action.to_asset} is neither a stablecoin nor a current holding")

    ok = not errors
    out = action.model_copy(update={"status": "validated" if ok else "rejected"})
    return ValidationResult(
        ok=ok, action=out, errors=errors, warnings=warnings,
        unverified=unverified, notional_usdc=notional,
    )
