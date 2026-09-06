"""The buyer agent: reads the sub-account, plans, pays sellers via x402, reports."""

from __future__ import annotations

from dataclasses import dataclass

from . import brain
from .binance_client import ASSETS, get_client
from .config import settings
from .models import Payment, Report, Signal
from .x402 import Ledger, PaymentError, paid_get

SELLER_PATHS = {"funding": "/analysis", "risk": "/analysis"}


@dataclass
class RunResult:
    report: Report
    ledger: Ledger


def _seller_params(key: str, portfolio) -> dict | None:
    if key == "risk":
        hint = ",".join(f"{h.asset}:{h.qty}" for h in portfolio.holdings)
        return {"holdings": hint}
    return None


def run(*, on_event=None) -> RunResult:
    """Execute one full research cycle. `on_event(str, dict)` is an optional UI hook."""
    emit = on_event or (lambda *a, **k: None)
    client = get_client()

    emit("connect", {"mode": settings.binance_mode, "mcp": settings.binance_mcp_url})
    portfolio = client.get_portfolio()
    market = client.get_market(ASSETS)
    emit("portfolio", {"portfolio": portfolio, "market": market})

    ledger = Ledger(balance_usdc=portfolio.cash_usdc)
    emit("budget", {"balance": ledger.balance_usdc, "cap": ledger.daily_cap_usdc})

    picks, rationale = brain.plan(portfolio, market)
    emit("plan", {"picks": picks, "rationale": rationale})

    signals: list[Signal] = []
    payments: list[Payment] = []
    for key in picks:
        base_url = settings.seller_urls[key]
        path = SELLER_PATHS[key]
        emit("buy_start", {"seller": key, "url": base_url + path})
        try:
            body, payment = paid_get(base_url, path, ledger, params=_seller_params(key, portfolio))
        except PaymentError as e:
            emit("buy_error", {"seller": key, "error": str(e)})
            continue
        except Exception as e:  # network / seller down
            emit("buy_error", {"seller": key, "error": f"{type(e).__name__}: {e}"})
            continue
        payments.append(payment)
        for raw in body.get("signals", []):
            try:
                signals.append(Signal(**raw))
            except Exception:
                continue
        emit(
            "buy_ok",
            {
                "seller": key,
                "payment": payment,
                "signals": len(body.get("signals", [])),
                "balance": ledger.balance_usdc,
            },
        )

    narrative, rebalance = brain.synthesize(portfolio, market, signals)
    emit("synthesize", {"narrative": narrative, "rebalance": rebalance})

    report = Report(
        sub_account=portfolio.sub_account,
        portfolio=portfolio,
        market=market,
        plan_rationale=rationale,
        payments=payments,
        signals=signals,
        narrative=narrative,
        rebalance=rebalance,
        spend_usdc=round(sum(p.amount_usdc for p in payments), 2),
        budget_usdc=ledger.daily_cap_usdc,
    )
    emit("done", {"report": report, "ledger": ledger})
    return RunResult(report=report, ledger=ledger)


def approve_rebalance(report: Report, index: int = 0) -> dict:
    """Execute one proposed Convert inside the sub-account (called after human 'y')."""
    if index >= len(report.rebalance):
        raise IndexError("no such rebalance action")
    return get_client().execute_convert(report.rebalance[index])
