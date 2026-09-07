"""A compact x402 implementation for agent-to-agent payments.

x402 (HTTP 402 "Payment Required") handshake:

    1. client  GET  /analysis
    2. server  402  { x402Version, accepts: [PaymentRequirements] }
    3. client  builds a signed payment payload, base64 -> `X-PAYMENT` header
    4. client  GET  /analysis            (with X-PAYMENT)
    5. server  verifies + settles, 200 + `X-PAYMENT-RESPONSE` header (tx hash)

Both modes use the `scheme: "exact"` EVM payload — an EIP-3009
``TransferWithAuthorization`` (``authorization`` + ``signature``).

* ``X402_MODE=mock`` — v1-shaped payload; the ``signature`` is an HMAC over the
  canonical authorization; verify recomputes it, settle fabricates a
  deterministic tx hash. No chain, no key; the ledger still moves so the demo
  shows the sub-account USDC draining and the daily cap biting.
* ``X402_MODE=live`` — x402 **v2** payload (``accepted`` + ``resource``, CAIP-2
  network); the ``signature`` is a real EIP-712 signature over the authorization
  (``X402_WALLET_PRIVATE_KEY``); the seller hands payload + requirements to a
  facilitator (`/verify`, `/settle`) which submits the gasless
  ``transferWithAuthorization`` on-chain and returns the tx hash. Default
  facilitator ``https://x402.org/facilitator`` (Base Sepolia).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import date

import httpx

from .config import settings
from .models import Payment

USDC_DECIMALS = 6
X402_VERSION = 1

# USDC (EIP-3009) contract addresses.
USDC_ADDRESS = {
    "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "base-sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
}

# x402 v1 legacy network name -> EVM chain id / CAIP-2 (used by v2 facilitators).
CHAIN_ID = {"base": 8453, "base-sepolia": 84532}
CAIP2 = {"base": "eip155:8453", "base-sepolia": "eip155:84532"}

# EIP-712 types for EIP-3009 TransferWithAuthorization.
_EIP712_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ],
}

# Payer address used in mock mode (no wallet key needed).
_MOCK_PAYER = "0xA11ce00000000000000000000000000000A1FA00"


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


def to_v2_requirements(d: dict) -> dict:
    """v1 requirements dict -> x402 v2 shape (CAIP-2 network, `amount`)."""
    return {
        "scheme": d["scheme"],
        "network": CAIP2.get(d["network"], d["network"]),
        "asset": d["asset"],
        "amount": d["maxAmountRequired"],
        "payTo": d["payTo"],
        "maxTimeoutSeconds": d.get("maxTimeoutSeconds", 600),
        "extra": d.get("extra", {}),
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


def _canonical_auth(auth: dict) -> bytes:
    return json.dumps(
        {k: str(auth[k]) for k in ("from", "to", "value", "validAfter", "validBefore", "nonce")},
        sort_keys=True,
    ).encode()


def _mock_signature(auth: dict) -> str:
    return "0x" + hmac.new(
        settings.x402_mock_secret.encode(), _canonical_auth(auth), hashlib.sha256
    ).hexdigest()


def _eip712_message(auth: dict, reqs_dict: dict) -> dict:
    net = reqs_dict["network"]
    extra = reqs_dict.get("extra") or {}
    return {
        "domain": {
            "name": extra.get("name", "USDC"),
            "version": extra.get("version", "2"),
            "chainId": CHAIN_ID.get(net, 84532),
            "verifyingContract": reqs_dict["asset"],
        },
        "types": _EIP712_TYPES,
        "primaryType": "TransferWithAuthorization",
        "message": {
            "from": auth["from"],
            "to": auth["to"],
            "value": int(auth["value"]),
            "validAfter": int(auth["validAfter"]),
            "validBefore": int(auth["validBefore"]),
            "nonce": bytes.fromhex(auth["nonce"][2:]),
        },
    }


def verify_payment_header(header_value: str, reqs: PaymentRequirements) -> tuple[bool, str, dict]:
    """Returns (ok, reason, decoded_payload)."""
    try:
        payload = json.loads(base64.b64decode(header_value))
    except Exception:
        return False, "malformed X-PAYMENT header", {}

    version = payload.get("x402Version")
    if version == 2:  # live: scheme/network live under `accepted`
        acc = payload.get("accepted", {})
        if acc.get("scheme") != reqs.scheme or acc.get("network") != CAIP2.get(reqs.network, reqs.network):
            return False, "scheme/network mismatch", payload
    elif version == X402_VERSION:
        if payload.get("scheme") != reqs.scheme or payload.get("network") != reqs.network:
            return False, "scheme/network mismatch", payload
    else:
        return False, "unsupported x402Version", payload

    inner = payload.get("payload", {})
    auth = inner.get("authorization", {})
    signature = inner.get("signature", "")
    try:
        if int(auth["value"]) < int(reqs.max_amount_required):
            return False, "authorization value below maxAmountRequired", payload
    except (KeyError, TypeError, ValueError):
        return False, "malformed authorization", payload
    if auth.get("to", "").lower() != reqs.pay_to.lower():
        return False, "authorization payTo mismatch", payload

    if settings.x402_mode == "live":
        return _facilitator_verify(payload, reqs)

    if not hmac.compare_digest(_mock_signature(auth), signature):
        return False, "bad signature", payload
    return True, "", payload


def settle_payment(payload: dict, reqs: PaymentRequirements) -> dict:
    """Returns settlement info: {success, txHash, network}."""
    if settings.x402_mode == "live":
        return _facilitator_settle(payload, reqs)
    nonce = payload.get("payload", {}).get("authorization", {}).get("nonce", "")
    seed = f"{nonce}:{reqs.max_amount_required}:{time.time_ns()}"
    tx = "0x" + hashlib.sha256(seed.encode()).hexdigest()
    return {"success": True, "txHash": tx, "network": reqs.network}


def settlement_header(settlement: dict) -> str:
    return base64.b64encode(json.dumps(settlement).encode()).decode()


def _facilitator_body(payload: dict, reqs: PaymentRequirements) -> dict:
    # payload is already a v2 PaymentPayload (see _build_payment_header live branch)
    return {
        "x402Version": 2,
        "paymentPayload": payload,
        "paymentRequirements": to_v2_requirements(reqs.to_dict()),
    }


def _facilitator_verify(payload: dict, reqs: PaymentRequirements) -> tuple[bool, str, dict]:  # pragma: no cover
    try:
        with httpx.Client(timeout=25.0) as c:
            r = c.post(f"{settings.x402_facilitator_url}/verify", json=_facilitator_body(payload, reqs))
            r.raise_for_status()
            data = r.json()
        return bool(data.get("isValid")), data.get("invalidReason") or data.get("invalidMessage") or "", payload
    except Exception as e:
        return False, f"facilitator verify failed: {e}", payload


def _facilitator_settle(payload: dict, reqs: PaymentRequirements) -> dict:  # pragma: no cover
    try:
        with httpx.Client(timeout=90.0) as c:
            r = c.post(f"{settings.x402_facilitator_url}/settle", json=_facilitator_body(payload, reqs))
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return {"success": False, "txHash": "", "network": reqs.network, "error": f"facilitator settle failed: {e}"}
    return {
        "success": bool(data.get("success")),
        "txHash": data.get("transaction", ""),
        "network": data.get("network", reqs.network),
        "payer": data.get("payer", ""),
        "error": data.get("errorReason") or data.get("errorMessage") or "",
    }


# --------------------------------------------------------------------- client side
class PaymentError(RuntimeError):
    pass


def _local_account():
    """eth_account LocalAccount from X402_WALLET_PRIVATE_KEY, or None."""
    key = settings.x402_wallet_private_key.strip()
    if not key:
        return None
    from eth_account import Account

    return Account.from_key(key if key.startswith("0x") else "0x" + key)


def payer_address() -> str:
    acct = _local_account()
    return acct.address if acct else _MOCK_PAYER


def _sign_eip712(auth: dict, reqs_dict: dict) -> str:
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    acct = _local_account()
    if acct is None:
        raise PaymentError("X402_MODE=live but X402_WALLET_PRIVATE_KEY is unset")
    signed = Account.sign_message(
        encode_typed_data(full_message=_eip712_message(auth, reqs_dict)), acct.key
    )
    return "0x" + signed.signature.hex().removeprefix("0x")


def _build_payment_header(reqs_dict: dict) -> str:
    now = int(time.time())
    auth = {
        "from": payer_address(),
        "to": reqs_dict["payTo"],
        "value": str(reqs_dict["maxAmountRequired"]),
        "validAfter": str(now - 600),
        "validBefore": str(now + int(reqs_dict.get("maxTimeoutSeconds", 600) or 600)),
        "nonce": "0x" + secrets.token_bytes(32).hex(),
    }
    if settings.x402_mode == "live":
        # x402 v2 PaymentPayload — what x402.org/facilitator and other v2
        # facilitators verify + settle on-chain (gasless EIP-3009).
        return base64.b64encode(
            json.dumps(
                {
                    "x402Version": 2,
                    "payload": {"signature": _sign_eip712(auth, reqs_dict), "authorization": auth},
                    "accepted": to_v2_requirements(reqs_dict),
                    "resource": {
                        "url": reqs_dict["resource"],
                        "description": reqs_dict.get("description", ""),
                        "mimeType": reqs_dict.get("mimeType", "application/json"),
                    },
                }
            ).encode()
        ).decode()

    payload = {
        "x402Version": X402_VERSION,
        "scheme": reqs_dict["scheme"],
        "network": reqs_dict["network"],
        "payload": {"authorization": auth, "signature": _mock_signature(auth)},
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def paid_get(base_url: str, path: str, ledger: Ledger, *, params: dict | None = None) -> tuple[dict, Payment]:
    """Do the full 402 handshake against `base_url + path`. Returns (json_body, Payment)."""
    url = base_url.rstrip("/") + path
    # sellers may fetch live public market data before answering; be generous.
    with httpx.Client(timeout=90.0) as c:
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
        if paid.status_code == 402:
            try:
                err = paid.json().get("error", "payment rejected")
            except Exception:
                err = "payment rejected"
            raise PaymentError(f"{url} rejected the payment: {err}")
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
