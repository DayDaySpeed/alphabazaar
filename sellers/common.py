"""Shared x402 paywall for the seller agents."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import Request
from fastapi.responses import JSONResponse

from alphabazaar import marketdata, x402
from alphabazaar.models import Signal


class PaywallResult:
    def __init__(self, response: JSONResponse | None, settlement: dict | None, requirements):
        self.response = response  # non-None -> return this immediately
        self.settlement = settlement
        self.requirements = requirements


def charge(request: Request, *, price_usdc: float, resource: str, description: str, pay_to: str) -> PaywallResult:
    """Enforce x402 on an incoming request.

    Returns a PaywallResult. If ``.response`` is set, the caller must return it
    (either a 402 challenge or a 402/verification error). Otherwise ``.settlement``
    holds the tx info to expose via the ``X-PAYMENT-RESPONSE`` header.
    """
    reqs = x402.make_requirements(
        price_usdc=price_usdc, resource=resource, description=description, pay_to=pay_to
    )
    header = request.headers.get("x-payment")
    if not header:
        return PaywallResult(
            JSONResponse(status_code=402, content=x402.payment_required_body(reqs)),
            None,
            reqs,
        )

    ok, reason, payload = x402.verify_payment_header(header, reqs)
    if not ok:
        return PaywallResult(
            JSONResponse(
                status_code=402,
                content=x402.payment_required_body(reqs, error=f"payment verification failed: {reason}"),
            ),
            None,
            reqs,
        )

    settlement = x402.settle_payment(payload, reqs)
    if not settlement.get("success"):
        return PaywallResult(
            JSONResponse(
                status_code=402,
                content=x402.payment_required_body(reqs, error="settlement failed"),
            ),
            None,
            reqs,
        )
    return PaywallResult(None, settlement, reqs)


def with_settlement(body: dict, settlement: dict) -> JSONResponse:
    return JSONResponse(
        content=body,
        headers={"X-PAYMENT-RESPONSE": x402.settlement_header(settlement)},
    )


def analysis_response(
    *,
    seller: str,
    methodology: str,
    signals: list[Signal],
    settlement: dict,
    horizon: str = "",
    valid_for_hours: float = 0.0,
) -> JSONResponse:
    """Build the paid 200 body with data provenance + per-signal traceability.

    ``provenance`` tells the buyer whether the numbers behind these signals came
    from live Binance data or a synthetic fallback. Each signal is also stamped
    with its seller id, the evidence it rests on (its metrics + the data window),
    and how long it is meant to stay actionable — so the report / workbench can
    trace any claim back to who made it, on what, and until when.
    """
    prov = marketdata.data_provenance()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    valid_until = (
        (now_dt + timedelta(hours=valid_for_hours)).isoformat(timespec="seconds")
        if valid_for_hours > 0
        else ""
    )
    stamped = []
    for s in signals:
        d = s.model_dump()
        d.setdefault("source", "binance-public-rest" if not prov["degraded"] else "synthetic-fallback")
        d["method"] = d.get("method") or methodology[:120]
        d["as_of"] = now
        d["seller_id"] = seller
        d["horizon"] = d.get("horizon") or horizon
        d["valid_until"] = d.get("valid_until") or valid_until
        if not d.get("evidence"):
            d["evidence"] = {
                "metrics": dict(d.get("metrics") or {}),
                "data_window": methodology[:160],
                "market_source": ",".join(prov["sources"]),
                "computed_at": now,
            }
        stamped.append(d)
    body = {
        "seller": seller,
        "generated_at": now,
        "methodology": methodology,
        "horizon": horizon,
        "valid_until": valid_until,
        "provenance": prov,
        "signals": stamped,
    }
    return with_settlement(body, settlement)
