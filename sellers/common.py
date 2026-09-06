"""Shared x402 paywall for the seller agents."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from alphabazaar import x402


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
