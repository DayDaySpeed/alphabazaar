"""Quote → approve → execute, with the approval bound to a specific quote.

Flow (see also ``analyst`` / ``cli``)::

    propose  →  validate  →  quote  →  show quote + expiry  →  human approves
             →  execute (once)  →  record result

An approval is a token over ``(account, action, quote)``. If the quote expires or
its terms change, the token no longer matches and the human must approve again.
The token is single-use — recorded in the ledger's ``executions`` table before
the order is sent, so the same approval can't drive two orders.

``mock`` / ``snapshot`` never place a real order (they return ``RECORDED`` /
``FILLED (mock)``). ``live`` execution additionally refuses when market/snapshot
data is stale or degraded.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from .config import settings
from .models import RebalanceAction

QUOTE_TTL_SECONDS = 30


class ExecutionError(RuntimeError):
    pass


@dataclass
class Quote:
    from_asset: str
    to_asset: str
    from_qty: float
    to_qty: float
    rate: float
    quote_id: str
    created_at: float
    expires_at: float
    verified: bool  # True only for a real venue quote (live MCP)
    source: str

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def as_dict(self) -> dict:
        return {
            "from": f"{self.from_qty:g} {self.from_asset}",
            "to": f"≈{self.to_qty:g} {self.to_asset}",
            "rate": self.rate,
            "quote_id": self.quote_id,
            "expires_in_s": max(0, round(self.expires_at - time.time())),
            "verified": self.verified,
            "source": self.source,
        }


def action_hash(account: str, action: RebalanceAction) -> str:
    payload = json.dumps(
        {
            "account": account,
            "from_asset": action.from_asset,
            "to_asset": action.to_asset,
            "from_qty": round(float(action.from_qty), 10),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def quote_hash(quote: Quote) -> str:
    payload = json.dumps(
        {
            "quote_id": quote.quote_id,
            "from_qty": round(quote.from_qty, 10),
            "to_qty": round(quote.to_qty, 10),
            "expires_at": round(quote.expires_at, 3),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def approval_token(account: str, action: RebalanceAction, quote: Quote) -> str:
    return hashlib.sha256(
        f"{action_hash(account, action)}:{quote_hash(quote)}".encode()
    ).hexdigest()


def get_quote(client, action: RebalanceAction) -> Quote:
    """Ask the venue for a quote; fall back to a clearly-unverified estimate.

    A client may implement ``quote_convert(action) -> dict`` (live MCP path).
    Otherwise we derive an estimate from the action's own numbers and mark it
    ``verified=False`` so the report and the human both see it isn't a real fill.
    """
    now = time.time()
    fn = getattr(client, "quote_convert", None)
    if callable(fn):
        q = fn(action)
        to_qty = float(q["to_qty"])
        qid = str(q.get("quote_id", "")).strip()
        # a "verified" quote must carry a real venue quote id — the approval binds
        # to it and execute() replays exactly that id, never a fresh one
        verified = bool(qid)
        ttl = float(q.get("ttl_s", QUOTE_TTL_SECONDS)) or QUOTE_TTL_SECONDS
        return Quote(
            from_asset=action.from_asset, to_asset=action.to_asset,
            from_qty=float(q.get("from_qty", action.from_qty)), to_qty=to_qty,
            rate=to_qty / float(action.from_qty) if action.from_qty else 0.0,
            quote_id=qid or f"unquoted-{int(now)}",
            created_at=now, expires_at=now + ttl,
            verified=verified,
            source="binance-mcp-convert" if verified else "venue returned no quote id",
        )
    est = float(action.est_to_qty)
    return Quote(
        from_asset=action.from_asset, to_asset=action.to_asset,
        from_qty=float(action.from_qty), to_qty=est,
        rate=est / float(action.from_qty) if action.from_qty else 0.0,
        quote_id=f"est-{int(now)}",
        created_at=now, expires_at=now + QUOTE_TTL_SECONDS,
        verified=False, source="estimate (no live quote available)",
    )


def execute(
    client,
    action: RebalanceAction,
    quote: Quote,
    token: str,
    *,
    account: str,
    run_id: str,
    ledger,
    binance_mode: str,
    data_degraded: bool = False,
    data_stale: bool = False,
) -> dict:
    """Execute one approved Convert. Raises ``ExecutionError`` on any guard failure."""
    expected = approval_token(account, action, quote)
    if token != expected:
        raise ExecutionError("approval does not match this account/action/quote — re-approve")
    if quote.expired:
        raise ExecutionError("quote expired — request a fresh quote and re-approve")

    if binance_mode == "live":
        if data_stale:
            raise ExecutionError("market/snapshot data is stale — live execution blocked")
        if data_degraded:
            raise ExecutionError("market data is degraded (synthetic fallback) — live execution blocked")
        if not quote.verified:
            raise ExecutionError("no verified venue quote — live execution blocked")

    first_use = ledger.record_execution_intent(
        approval_token=token, run_id=run_id, account=account,
        action_hash=action_hash(account, action), quote_id=quote.quote_id,
    )
    if not first_use:
        raise ExecutionError("this approval was already used — refusing to execute again")

    try:
        # live path executes THE approved quote (by id), never a fresh one;
        # mock/snapshot ignore quote_id
        result = client.execute_convert(action, quote_id=quote.quote_id if quote.verified else None)
    except Exception as e:
        ledger.update_execution(token, status="unknown", detail=f"{type(e).__name__}: {e}")
        raise ExecutionError(f"execution result unknown: {e}") from e

    status = str(result.get("status", "")).upper()
    if status in ("FILLED", "SUCCESS"):
        led_status = "filled"
    elif status in ("RECORDED", "SUBMITTED", "ACCEPTED", "PROCESS", "ACCEPT_SUCCESS"):
        led_status = "submitted"
    elif status in ("FAILED", "REJECTED", "ERROR", "FAIL", "EXPIRED"):
        led_status = "failed"
    else:
        led_status = "unknown"
    ledger.update_execution(token, status=led_status, detail=json.dumps(result)[:500])
    result["approval_token"] = token
    result["ledger_status"] = led_status
    return result
