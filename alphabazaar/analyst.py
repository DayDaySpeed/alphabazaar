"""The buyer agent: reads the sub-account, plans, pays sellers via x402, reports."""

from __future__ import annotations

from dataclasses import dataclass

from . import brain
from .binance_client import ASSETS, get_client
from .config import settings
from .models import Payment, Report, Signal
from .registry import load_sellers
from .x402 import Ledger, PaymentError, paid_get


@dataclass
class RunResult:
    report: Report
    ledger: Ledger


def _holdings_hint(portfolio) -> dict:
    return {"holdings": ",".join(f"{h.asset}:{h.qty}" for h in portfolio.holdings)}


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

    sellers = load_sellers()
    by_id = {e["id"]: e for e in sellers}
    emit("discover", {"sellers": sellers})

    picks, rationale = brain.plan(portfolio, market, sellers)
    emit("plan", {"picks": picks, "rationale": rationale})

    signals: list[Signal] = []
    payments: list[Payment] = []
    hint = _holdings_hint(portfolio)
    for pid in picks:
        entry = by_id.get(pid)
        if not entry:
            continue
        emit("buy_start", {"seller": pid, "url": entry["endpoint"]})
        try:
            body, payment = paid_get(entry["url"], entry["path"], ledger, params=hint)
        except PaymentError as e:
            emit("buy_error", {"seller": pid, "error": str(e)})
            continue
        except Exception as e:  # network / seller down
            emit("buy_error", {"seller": pid, "error": f"{type(e).__name__}: {e}"})
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
                "seller": pid,
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
