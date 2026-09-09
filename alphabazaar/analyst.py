"""The buyer agent: reads the sub-account, plans, pays sellers via x402, reports.

Reliability surface (why this file is more than a loop):

* budget + payments live in a persistent SQLite ledger (``alphabazaar.ledger``),
  keyed per payer-identity / network / UTC-day — survives restarts and rolls over
  at midnight UTC. See ``x402.paid_get``.
* data provenance (live vs synthetic vs stale snapshot) is collected and carried
  into the report; degraded / stale data blocks live trade execution.
* every proposed rebalance is re-validated deterministically (``validate.py``)
  before it can be quoted, approved or executed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

from . import brain, marketdata, ratings
from .binance_client import ASSETS, get_client
from .config import settings
from .execution import ExecutionError, Quote, approval_token, execute, get_quote
from .ledger import BudgetLedger
from .models import DataProvenance, Payment, Report, Signal
from .registry import load_sellers
from .validate import validate_rebalance
from .x402 import PaymentError, paid_get, payer_address


@dataclass
class RunResult:
    report: Report
    run_id: str
    budget: BudgetLedger
    client: object
    validations: list = field(default_factory=list)

    # kept for backwards compat with older callers / tests
    @property
    def ledger(self) -> BudgetLedger:
        return self.budget


def _holdings_hint(portfolio) -> dict:
    # free balance only — locked coins can't be traded, so don't advertise them
    return {"holdings": ",".join(f"{h.asset}:{h.free}" for h in portfolio.holdings)}


def _collect_provenance(client, seller_bodies: list[dict]) -> tuple[list[DataProvenance], bool, bool]:
    prov: list[DataProvenance] = []
    degraded = False
    stale = False
    for entry in getattr(client, "data_provenance", list)() or []:
        dp = DataProvenance(
            label=entry.get("label", "data"),
            source=entry.get("source", "unknown"),
            as_of=entry.get("as_of") or _utc_iso(),
            captured_at=entry.get("captured_at"),
            degraded=bool(entry.get("degraded")),
            stale=bool(entry.get("stale")),
            reason=entry.get("reason", ""),
        )
        prov.append(dp)
        degraded = degraded or dp.degraded
        stale = stale or dp.stale
    for body in seller_bodies:
        p = body.get("provenance") or {}
        if not p:
            prov.append(DataProvenance(
                label=f"seller:{body.get('seller', '?')}", source="unknown",
                degraded=True, reason="seller response carried no provenance (older seller)",
            ))
            degraded = True
            continue
        d = bool(p.get("degraded"))
        prov.append(DataProvenance(
            label=f"seller:{body.get('seller', '?')}",
            source=",".join(p.get("sources", [])) or "unknown",
            as_of=p.get("as_of", ""), degraded=d,
            reason="" if not d else "seller used a synthetic market-data fallback",
        ))
        degraded = degraded or d
    return prov, degraded, stale


def run(*, on_event=None, resume_run_id: str | None = None) -> RunResult:
    """Execute one full research cycle. `on_event(str, dict)` is an optional UI hook."""
    emit = on_event or (lambda *a, **k: None)
    marketdata.reset_provenance()
    run_id = resume_run_id or uuid.uuid4().hex[:12]
    client = get_client()

    emit("connect", {"mode": settings.binance_mode, "mcp": settings.binance_mcp_url, "run_id": run_id})
    portfolio = client.get_portfolio()
    market = client.get_market(ASSETS)
    emit("portfolio", {"portfolio": portfolio, "market": market})

    budget = BudgetLedger(
        payment_identity=payer_address(),
        network=settings.x402_network,
        daily_cap_usdc=settings.x402_daily_cap_usdc,
        db_path=settings.ledger_db,
    )
    emit("budget", {
        "identity": budget.payment_identity,
        "network": budget.network,
        "remaining": budget.remaining_usdc(),
        "cap": settings.x402_daily_cap_usdc,
        "trading_cash": portfolio.cash_usdc,
    })

    sellers = load_sellers()
    by_id = {e["id"]: e for e in sellers}
    seller_scores = ratings.seller_scores(budget)
    emit("discover", {"sellers": sellers, "scores": seller_scores})

    picks, rationale = brain.plan(
        portfolio, market, sellers,
        budget_remaining_usdc=budget.remaining_usdc(),
        seller_scores=seller_scores,
    )
    picks = list(dict.fromkeys(picks))  # de-dup duplicate seller selections
    emit("plan", {"picks": picks, "rationale": rationale, "scores": seller_scores})

    signals: list[Signal] = []
    payments: list[Payment] = []
    seller_bodies: list[dict] = []
    seller_errors: list[str] = []
    delivered = 0
    hint = _holdings_hint(portfolio)

    for pid in picks:
        entry = by_id.get(pid)
        if not entry:
            seller_errors.append(f"{pid}: not in registry")
            continue
        emit("buy_start", {"seller": pid, "url": entry["endpoint"]})
        try:
            body, payment = paid_get(
                entry["url"], entry["path"], budget,
                run_id=run_id, seller_id=pid, params=hint,
            )
        except PaymentError as e:
            seller_errors.append(f"{pid}: {e}")
            emit("buy_error", {"seller": pid, "error": str(e)})
            continue
        except Exception as e:  # network / seller down
            seller_errors.append(f"{pid}: {type(e).__name__}: {e}")
            emit("buy_error", {"seller": pid, "error": f"{type(e).__name__}: {e}"})
            continue

        payments.append(payment)
        seller_bodies.append(body)
        budget.mark_provenance(
            payment.request_id,
            degraded=bool((body.get("provenance") or {}).get("degraded")),
        )
        n = 0
        for raw in body.get("signals", []):
            try:
                sig = Signal(**raw)
            except Exception:
                continue
            # bind every signal to the registry id we chose + paid, NOT the
            # seller's self-reported name (which it must not get to spoof)
            sig.seller_id = pid
            sig.request_id = payment.request_id
            if not sig.evidence:
                sig.evidence = {"metrics": dict(sig.metrics), "note": "seller sent no evidence block"}
            signals.append(sig)
            n += 1
        if payment.delivery_status == "delivered":
            delivered += 1
        emit("buy_ok", {
            "seller": pid, "payment": payment, "signals": n,
            "remaining": budget.remaining_usdc(),
        })

    provenance, data_degraded, data_stale = _collect_provenance(client, seller_bodies)
    analysis_complete = bool(picks) and delivered == len(picks)

    narrative, rebalance = brain.synthesize(
        portfolio, market, signals, analysis_complete=analysis_complete
    )
    emit("synthesize", {"narrative": narrative, "rebalance": rebalance})

    # ---- deterministic re-validation of every proposed action --------------
    validations = []
    validated_actions = []
    for act in rebalance:
        v = validate_rebalance(act, portfolio)
        validations.append(v)
        validated_actions.append(v.action)
        if not v.ok:
            seller_errors.append(f"rebalance rejected: {act.from_asset}->{act.to_asset}: "
                                 + "; ".join(v.errors))
    emit("validate", {"validations": [v.as_dict() for v in validations]})

    unresolved = budget.unresolved(run_id=run_id)

    block_reasons: list[str] = []
    if settings.binance_mode in ("mock", "snapshot"):
        block_reasons.append(f"{settings.binance_mode} mode never places real orders")
    if data_stale:
        block_reasons.append("data is stale (older than the freshness window)")
    if data_degraded:
        block_reasons.append("data is degraded (a synthetic fallback was used)")
    if not analysis_complete:
        block_reasons.append(
            f"analysis incomplete ({delivered}/{len(picks)} seller agents delivered)")
    if unresolved:
        block_reasons.append(f"{len(unresolved)} payment(s) in an unresolved state")

    report = Report(
        run_id=run_id,
        binance_mode=settings.binance_mode,
        x402_mode=settings.x402_mode,
        sub_account=portfolio.sub_account,
        portfolio=portfolio,
        market=market,
        plan_rationale=rationale,
        payments=payments,
        signals=signals,
        narrative=narrative,
        rebalance=validated_actions,
        spend_usdc=round(sum(p.amount_usdc for p in payments if p.pay_status != "failed"), 6),
        budget_usdc=settings.x402_daily_cap_usdc,
        budget_remaining_usdc=budget.remaining_usdc(),
        provenance=provenance,
        data_degraded=data_degraded,
        data_stale=data_stale,
        sellers_attempted=len(picks),
        sellers_delivered=delivered,
        seller_errors=seller_errors,
        analysis_complete=analysis_complete,
        llm_status=brain.last_status(),
        unresolved_payments=unresolved,
        trade_execution_allowed=not block_reasons,
        trade_block_reasons=block_reasons,
    )
    # log this run's signals so their real-world accuracy can be scored later
    # (cli score-signals). Bookkeeping only — never fail a run over it.
    try:
        from . import signal_scoring

        signal_scoring.record_run_signals(budget, run_id=run_id, market=market, signals=signals)
    except Exception as e:  # pragma: no cover
        emit("warn", {"where": "record_run_signals", "error": f"{type(e).__name__}: {e}"})

    emit("done", {"report": report, "budget": budget})
    return RunResult(report=report, run_id=run_id, budget=budget, client=client,
                     validations=validations)


# --------------------------------------------------------------- approval flow
def quote_for(result: RunResult, index: int = 0) -> tuple[Quote, object]:
    """Get a quote for proposed action #index. Returns (quote, action)."""
    actions = [v.action for v in result.validations if v.ok]
    if index >= len(actions):
        raise IndexError("no such validated rebalance action")
    action = actions[index]
    return get_quote(result.client, action), action


def approve_and_execute(result: RunResult, quote: Quote, action, *, human_approved: bool) -> dict:
    """Bind the human's approval to this exact quote, then execute once."""
    if not human_approved:
        raise ExecutionError("not approved by a human")
    account = result.report.sub_account
    token = approval_token(account, action, quote)
    return execute(
        result.client, action, quote, token,
        account=account, run_id=result.run_id, ledger=result.budget,
        binance_mode=settings.binance_mode,
        data_degraded=result.report.data_degraded,
        data_stale=result.report.data_stale,
    )


def approve_rebalance(report: Report, index: int = 0) -> dict:  # pragma: no cover - legacy shim
    """Deprecated: kept so old imports don't break. Prefer quote_for/approve_and_execute."""
    raise NotImplementedError(
        "approve_rebalance() was replaced by the quote→approve→execute flow; "
        "use analyst.quote_for() + analyst.approve_and_execute()"
    )
