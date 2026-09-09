"""Seller reputation, aggregated from the payment ledger.

The buyer discovers sellers from a registry it doesn't control. This module turns
the durable payment history (``alphabazaar.ledger``) into a per-seller track
record — how often a payment to them actually settled, whether they delivered
analysis for it, how often the answer rested on degraded data, and what they
charge — plus a single 0..1 reliability score the planner can use to rank or
skip a seller.

Signal *accuracy* (did the call turn out right?) needs signal outcomes to be
recorded over time; that is not tracked yet, so ``hit_rate`` is ``None`` and
flagged ``"pending"``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ledger import atomic_to_usdc

# statuses
_SETTLED = "settled"
_UNKNOWN = "settlement_unknown"
_FAILED = "failed"

# a score is only "confident" once we have this many calls of history
MIN_CALLS_FOR_CONFIDENCE = 3
# planner skips a seller whose confident score is below this
LOW_SCORE_THRESHOLD = 0.5


@dataclass
class SellerStat:
    seller_id: str
    calls: int = 0
    settled: int = 0
    settlement_unknown: int = 0
    failed: int = 0
    delivered: int = 0
    degraded: int = 0
    _price_atomic_sum: int = 0
    _price_atomic_n: int = 0
    hit_rate: float | None = None  # directional signal accuracy (None == none scored yet)
    signals_scored: int = 0

    # -- rates (0..1) --
    @property
    def fill_rate(self) -> float:
        return self.settled / self.calls if self.calls else 0.0

    @property
    def delivery_rate(self) -> float:
        return self.delivered / self.calls if self.calls else 0.0

    @property
    def unknown_rate(self) -> float:
        return self.settlement_unknown / self.calls if self.calls else 0.0

    @property
    def failure_rate(self) -> float:
        return self.failed / self.calls if self.calls else 0.0

    @property
    def degraded_rate(self) -> float:
        return self.degraded / self.calls if self.calls else 0.0

    @property
    def avg_price_usdc(self) -> float:
        return atomic_to_usdc(self._price_atomic_sum // self._price_atomic_n) if self._price_atomic_n else 0.0

    @property
    def confident(self) -> bool:
        return self.calls >= MIN_CALLS_FOR_CONFIDENCE

    @property
    def score(self) -> float:
        """0..1 reliability. Neutral 0.6 until there's enough history.

        Operational reliability (delivery / settlement / data quality) is the
        base; once directional signals have been scored, their hit-rate is
        blended in at 15%.
        """
        if not self.calls:
            return 0.6
        raw = (
            0.40 * self.delivery_rate
            + 0.30 * (1.0 - self.unknown_rate)
            + 0.20 * self.fill_rate
            + 0.10 * (1.0 - self.degraded_rate)
        )
        if not self.confident:
            # shrink toward the 0.6 prior when we have little data
            raw = raw * (self.calls / MIN_CALLS_FOR_CONFIDENCE) + 0.6 * (
                1 - self.calls / MIN_CALLS_FOR_CONFIDENCE
            )
        if self.hit_rate is not None:
            raw = 0.85 * raw + 0.15 * self.hit_rate
        return round(raw, 3)

    def as_dict(self) -> dict:
        return {
            "seller_id": self.seller_id,
            "calls": self.calls,
            "settled": self.settled,
            "settlement_unknown": self.settlement_unknown,
            "failed": self.failed,
            "delivered": self.delivered,
            "fill_rate": round(self.fill_rate, 3),
            "delivery_rate": round(self.delivery_rate, 3),
            "unknown_rate": round(self.unknown_rate, 3),
            "degraded_rate": round(self.degraded_rate, 3),
            "avg_price_usdc": round(self.avg_price_usdc, 2),
            "hit_rate": self.hit_rate,
            "signals_scored": self.signals_scored,
            "hit_rate_status": "none scored yet" if self.hit_rate is None else "tracked",
            "score": self.score,
            "confident": self.confident,
        }


def _accumulate(stats: dict[str, SellerStat], rows: list[dict]) -> None:
    for r in rows:
        sid = r.get("seller_id") or "?"
        st = stats.setdefault(sid, SellerStat(seller_id=sid))
        st.calls += 1
        status = r.get("pay_status")
        if status == _SETTLED:
            st.settled += 1
        elif status == _UNKNOWN:
            st.settlement_unknown += 1
        elif status == _FAILED:
            st.failed += 1
        if r.get("delivery_status") == "delivered":
            st.delivered += 1
        if r.get("data_degraded"):
            st.degraded += 1
        amt = r.get("amount_atomic")
        if amt:
            st._price_atomic_sum += int(amt)
            st._price_atomic_n += 1


def seller_stats(ledger, *, since: str | None = None) -> dict[str, SellerStat]:
    """Aggregate every payment row + scored signal into per-seller stats."""
    stats: dict[str, SellerStat] = {}
    _accumulate(stats, ledger.all_payments(since=since))
    try:
        hit_rates = ledger.signal_hit_rates()
    except Exception:
        hit_rates = {}
    for sid, hr in hit_rates.items():
        st = stats.setdefault(sid, SellerStat(seller_id=sid))
        st.hit_rate = hr.get("hit_rate")
        st.signals_scored = hr.get("scored", 0)
    return stats


def seller_scores(ledger, *, since: str | None = None) -> dict[str, float]:
    """seller_id -> 0..1 score, for the planner."""
    return {sid: st.score for sid, st in seller_stats(ledger, since=since).items()}


def ranked(ledger, registry_sellers: list[dict], *, since: str | None = None) -> list[dict]:
    """Registry entries + their stats, best score first (unknown sellers last-ish at 0.6)."""
    stats = seller_stats(ledger, since=since)
    out = []
    for e in registry_sellers:
        st = stats.get(e["id"]) or SellerStat(seller_id=e["id"])
        out.append({**e, "stats": st.as_dict()})
    out.sort(key=lambda x: x["stats"]["score"], reverse=True)
    return out
