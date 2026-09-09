"""Score purchased signals against what the market actually did — the missing
'was the seller right?' dimension of ``ratings``.

Deterministic and offline-testable: direction is derived from the signal ``kind``,
and the hit/miss call is a pure threshold on the realised price move over the
signal's stated ``valid_until`` horizon.

* ``record_run_signals`` — called by ``analyst.run`` after a purchase; logs each
  directional signal at ``outcome='pending'`` with the asset's price at issue.
* ``score_pending`` — called by ``cli score-signals``; for every pending signal
  now past ``valid_until``, compares the current price and marks
  hit / miss / neutral (or ``unscorable`` if a price is missing).
"""

from __future__ import annotations

import hashlib

from .ledger import utc_now_iso
from .models import MarketQuote, Signal

# realised move (in %) below which a directional call is neither right nor wrong
DECISIVE_MOVE_PCT = 1.0
# assets that aren't a single tradeable symbol — not scorable
_NON_ASSET = {"", "PORTFOLIO", "MARKET"}


def direction_of(kind: str) -> str:
    """opportunity -> bullish, risk -> bearish, info -> neutral."""
    return {"opportunity": "bullish", "risk": "bearish"}.get(kind, "neutral")


def classify(direction: str, move_pct: float, *, threshold: float = DECISIVE_MOVE_PCT) -> str:
    """hit / miss / neutral for a directional call given the realised move."""
    if direction == "neutral" or abs(move_pct) < threshold:
        return "neutral"
    if direction == "bullish":
        return "hit" if move_pct >= threshold else "miss"
    return "hit" if move_pct <= -threshold else "miss"  # bearish


def signal_id(run_id: str, seller_id: str, asset: str, title: str) -> str:
    return hashlib.sha256(f"{run_id}|{seller_id}|{asset}|{title}".encode()).hexdigest()


def record_run_signals(
    ledger, *, run_id: str, market: list[MarketQuote], signals: list[Signal]
) -> int:
    """Log this run's signals for later scoring. Returns how many were recorded."""
    price = {q.symbol[:-4] if q.symbol.endswith("USDT") else q.symbol: q.price for q in market}
    n = 0
    for s in signals:
        if s.asset in _NON_ASSET:
            continue
        ledger.record_signal(
            signal_id=signal_id(run_id, s.seller_id or "?", s.asset, s.title),
            run_id=run_id,
            request_id=s.request_id,
            seller_id=s.seller_id or "?",
            asset=s.asset,
            kind=s.kind,
            direction=direction_of(s.kind),
            ref_price=price.get(s.asset, 0.0),
            issued_at=s.as_of or utc_now_iso(),
            horizon=s.horizon,
            valid_until=s.valid_until,
        )
        n += 1
    return n


def score_pending(ledger, *, price_fn, now_iso: str | None = None) -> dict:
    """Score every pending signal past its horizon. ``price_fn(asset) -> float|None``."""
    now = now_iso or utc_now_iso()
    tally = {"hit": 0, "miss": 0, "neutral": 0, "unscorable": 0}
    for row in ledger.pending_signals(due_by=now):
        cur = None
        try:
            cur = price_fn(row["asset"])
        except Exception:
            cur = None
        ref = float(row["ref_price"] or 0)
        if not cur or ref <= 0:
            ledger.score_signal(row["signal_id"], outcome="unscorable",
                                scored_price=cur or 0, move_pct=0.0,
                                note="missing ref or current price")
            tally["unscorable"] += 1
            continue
        move = (float(cur) / ref - 1.0) * 100.0
        outcome = classify(row["direction"], move)
        ledger.score_signal(row["signal_id"], outcome=outcome,
                            scored_price=float(cur), move_pct=round(move, 3))
        tally[outcome] += 1
    return tally
