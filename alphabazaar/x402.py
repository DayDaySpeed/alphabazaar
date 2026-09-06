"""A compact x402 implementation for agent-to-agent payments.

x402 (HTTP 402 "Payment Required") handshake:

    1. client  GET  /analysis
    2. server  402  { x402Version, accepts: [PaymentRequirements] }
    3. client  builds a signed payment payload, base64 -> `X-PAYMENT` header
    4. client  GET  /analysis            (with X-PAYMENT)
    5. server  verifies + settles, 200 + `X-PAYMENT-RESPONSE` header (tx hash)

`X402_MODE=mock` signs/verifies with an HMAC over a shared secret and fabricates
a deterministic tx hash — no chain needed, but the ledger still moves so the
demo shows the sub-account balance draining. `X402_MODE=live` is where you call
a real facilitator (`/verify`, `/settle`) with an EIP-3009 `transferWithAuthorization`
payload signed by `X402_WALLET_PRIVATE_KEY`; the hook points are marked below.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from datetime import date

import httpx

from .config import settings
from .models import Payment

USDC_DECIMALS = 6
X402_VERSION = 1

# Demo USDC contract addresses (Base). Only cosmetic in mock mode.
USDC_ADDRESS = {
    "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "base-sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
}


def to_atomic(amount_usdc: float) -> str:
    return str(int(round(amount_usdc * 10**USDC_DECIMALS)))


def from_atomic(atomic: str | int) -> float:
    return int(atomic) / 10**USDC_DECIMALS


# --------------------------------------------------------------------------- ledger
@dataclass
class Ledger:
    """Mirrors spendable USDC in the Binance Agentic sub-account + the x402 daily cap."""

    balance_usdc: float
    daily_cap_usdc: float = field(default_factory=lambda: settings.x402_daily_cap_usdc)
    payments: list[Payment] = field(default_factory=list)
    _day: str = field(default_factory=lambda: date.today().isoformat())

    @property
    def spent_today(self) -> float:
        return round(sum(p.amount_usdc for p in self.payments if p.at[:10] == self._day), 6)

    @property
    def cap_remaining(self) -> float:
        return round(self.daily_cap_usdc - self.spent_today, 6)

    def can_afford(self, amount_usdc: float) -> tuple[bool, str]:
        if amount_usdc > self.balance_usdc:
            return False, f"insufficient sub-account balance ({self.balance_usdc:.2f} USDC)"
        if amount_usdc > self.cap_remaining:
            return False, f"x402 daily cap reached ({self.cap_remaining:.2f} USDC left of {self.daily_cap_usdc:.0f})"
        return True, ""

    def record(self, payment: Payment) -> None:
        self.balance_usdc = round(self.balance_usdc - payment.amount_usdc, 6)
        self.payments.append(payment)


# --------------------------------------------------------------------- server side
@dataclass
class PaymentRequirements:
    scheme: str
    network: str
    max_amount_required: str  # atomic units
    resource: str
    description: str
    pay_to: str
    asset: str
    mime_type: str = "application/json"
    max_timeout_seconds: int = 90
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "scheme": self.scheme,
            "network": self.network,
            "maxAmountRequired": self.max_amount_required,
            "resource": self.resource,
            "description": self.description,
            "payTo": self.pay_to,
            "asset": self.asset,
            "mimeType": self.mime_type,
            "maxTimeoutSeconds": self.max_timeout_seconds,
            "extra": self.extra,
        }


def make_requirements(*, price_usdc: float, resource: str, description: str, pay_to: str) -> PaymentRequirements:
    net = settings.x402_network
    return PaymentRequirements(
        scheme="exact",
        network=net,
        max_amount_required=to_atomic(price_usdc),
        resource=resource,
        description=description,
        pay_to=pay_to,
        asset=USDC_ADDRESS.get(net, USDC_ADDRESS["base-sepolia"]),
        extra={"name": "USDC", "version": "2"},
    )


def payment_required_body(reqs: PaymentRequirements, error: str = "payment required") -> dict:
    return {"x402Version": X402_VERSION, "error": error, "accepts": [reqs.to_dict()]}


def _mock_signature(reqs_dict: dict, payer: str, nonce: str) -> str:
    msg = json.dumps(
        {"payTo": reqs_dict["payTo"], "amount": reqs_dict["maxAmountRequired"], "payer": payer, "nonce": nonce},
        sort_keys=True,
    ).encode()
    return hmac.new(settings.x402_mock_secret.encode(), msg, hashlib.sha256).hexdigest()


def verify_payment_header(header_value: str, reqs: PaymentRequirements) -> tuple[bool, str, dict]:
    """Returns (ok, reason, decoded_payload)."""
    try:
        payload = json.loads(base64.b64decode(header_value))
    except Exception:
        return False, "malformed X-PAYMENT header", {}

    if payload.get("x402Version") != X402_VERSION:
        return False, "unsupported x402Version", payload
    if payload.get("scheme") != reqs.scheme or payload.get("network") != reqs.network:
        return False, "scheme/network mismatch", payload

    inner = payload.get("payload", {})
    if int(inner.get("amount", 0)) < int(reqs.max_amount_required):
        return False, "amount below maxAmountRequired", payload

    if settings.x402_mode == "live":
        # HOOK: POST reqs + payload to `${X402_FACILITATOR_URL}/verify`
        # and return its verdict.
        return _facilitator_verify(payload, reqs)

    expected = _mock_signature(reqs.to_dict(), inner.get("payer", ""), inner.get("nonce", ""))
    if not hmac.compare_digest(expected, inner.get("signature", "")):
        return False, "bad signature", payload
    return True, "", payload


def settle_payment(payload: dict, reqs: PaymentRequirements) -> dict:
    """Returns settlement info: {success, txHash, network}."""
    if settings.x402_mode == "live":
        # HOOK: POST to `${X402_FACILITATOR_URL}/settle`
        return _facilitator_settle(payload, reqs)
    seed = f"{payload.get('payload', {}).get('nonce', '')}:{reqs.max_amount_required}:{time.time_ns()}"
    tx = "0x" + hashlib.sha256(seed.encode()).hexdigest()
    return {"success": True, "txHash": tx, "network": reqs.network}


def settlement_header(settlement: dict) -> str:
    return base64.b64encode(json.dumps(settlement).encode()).decode()


def _facilitator_verify(payload: dict, reqs: PaymentRequirements) -> tuple[bool, str, dict]:  # pragma: no cover
    try:
        with httpx.Client(timeout=20.0) as c:
            r = c.post(
                f"{settings.x402_facilitator_url}/verify",
                json={"x402Version": X402_VERSION, "paymentPayload": payload, "paymentRequirements": reqs.to_dict()},
            )
            r.raise_for_status()
            data = r.json()
        return bool(data.get("isValid")), data.get("invalidReason", ""), payload
    except Exception as e:
        return False, f"facilitator verify failed: {e}", payload


def _facilitator_settle(payload: dict, reqs: PaymentRequirements) -> dict:  # pragma: no cover
    with httpx.Client(timeout=30.0) as c:
        r = c.post(
            f"{settings.x402_facilitator_url}/settle",
            json={"x402Version": X402_VERSION, "paymentPayload": payload, "paymentRequirements": reqs.to_dict()},
        )
        r.raise_for_status()
        data = r.json()
    return {"success": bool(data.get("success")), "txHash": data.get("transaction", ""), "network": reqs.network}


# --------------------------------------------------------------------- client side
class PaymentError(RuntimeError):
    pass


def _build_payment_header(reqs_dict: dict) -> str:
    nonce = hashlib.sha256(f"{time.time_ns()}:{reqs_dict['resource']}".encode()).hexdigest()[:32]
    payer = "0xA11CEabazaar000000000000000000000000A11CE"
    inner = {
        "amount": reqs_dict["maxAmountRequired"],
        "payTo": reqs_dict["payTo"],
        "asset": reqs_dict["asset"],
        "payer": payer,
        "nonce": nonce,
        "validAfter": 0,
        "validBefore": int(time.time()) + reqs_dict.get("maxTimeoutSeconds", 90),
    }
    if settings.x402_mode == "live":
        # HOOK: sign an EIP-3009 transferWithAuthorization with
        # X402_WALLET_PRIVATE_KEY and put the signature + authorization here.
        inner["signature"] = "<eip3009-signature>"
    else:
        inner["signature"] = _mock_signature(reqs_dict, payer, nonce)
    payload = {
        "x402Version": X402_VERSION,
        "scheme": reqs_dict["scheme"],
        "network": reqs_dict["network"],
        "payload": inner,
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def paid_get(base_url: str, path: str, ledger: Ledger, *, params: dict | None = None) -> tuple[dict, Payment]:
    """Do the full 402 handshake against `base_url + path`. Returns (json_body, Payment)."""
    url = base_url.rstrip("/") + path
    with httpx.Client(timeout=30.0) as c:
        first = c.get(url, params=params)
        if first.status_code != 402:
            if first.status_code == 200:
                raise PaymentError(f"{url} did not require payment (got 200) — expected a paywall")
            first.raise_for_status()

        body = first.json()
        try:
            reqs_dict = body["accepts"][0]
        except (KeyError, IndexError) as e:
            raise PaymentError(f"malformed 402 body from {url}: {body}") from e

        price = from_atomic(reqs_dict["maxAmountRequired"])
        ok, reason = ledger.can_afford(price)
        if not ok:
            raise PaymentError(f"cannot pay {price:.2f} USDC for {path}: {reason}")

        header = _build_payment_header(reqs_dict)
        paid = c.get(url, params=params, headers={"X-PAYMENT": header})
        paid.raise_for_status()

    settlement = {}
    resp_header = paid.headers.get("x-payment-response")
    if resp_header:
        try:
            settlement = json.loads(base64.b64decode(resp_header))
        except Exception:
            settlement = {}

    payment = Payment(
        seller=reqs_dict.get("description", path),
        resource=reqs_dict.get("resource", url),
        amount_usdc=price,
        network=reqs_dict["network"],
        tx_hash=settlement.get("txHash", "0x" + "0" * 64),
        settled=bool(settlement.get("success", settings.x402_mode == "mock")),
        mode="live" if settings.x402_mode == "live" else "mock",
    )
    ledger.record(payment)
    return paid.json(), payment
