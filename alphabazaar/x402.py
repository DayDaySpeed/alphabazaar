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
import ipaddress
import json
import secrets
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

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


# The budget + payment ledger now lives in ``alphabazaar.ledger.BudgetLedger``
# (persistent SQLite, per payer-identity / network / UTC-day). ``paid_get`` below
# takes one.


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
        valid_after = int(auth["validAfter"])
        valid_before = int(auth["validBefore"])
    except (KeyError, TypeError, ValueError):
        return False, "malformed authorization", payload
    if auth.get("to", "").lower() != reqs.pay_to.lower():
        return False, "authorization payTo mismatch", payload
    now = int(time.time())
    if now < valid_after:
        return False, "authorization not yet valid (validAfter in the future)", payload
    if now >= valid_before:
        return False, "authorization expired (validBefore in the past)", payload

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


# always refused — cloud metadata / link-local / unroutable
_BLOCKED_NETS = tuple(
    ipaddress.ip_network(n) for n in (
        "169.254.0.0/16", "fe80::/10",   # link-local (incl. 169.254.169.254 metadata)
        "0.0.0.0/8", "::/128",           # unspecified / "this host"
        "224.0.0.0/4", "ff00::/8",       # multicast
    )
)
# refused unless X402_ALLOW_PRIVATE_SELLERS=1 — classic LAN / internal ranges
_PRIVATE_NETS = tuple(
    ipaddress.ip_network(n) for n in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
    )
)
# NOT blocked: 198.18.0.0/15 and 100.64.0.0/10 — commonly handed back by VPN /
# tunneling resolvers (Cloudflare WARP etc.) as routing sentinels for otherwise
# public hosts; blocking them breaks legitimate use.


def _guard_seller_url(url: str) -> None:
    """Refuse to send a payment to a metadata / link-local / (by default) LAN host.

    The seller list is operator config, but a typo or a compromised registry
    entry shouldn't let the buyer hit ``169.254.169.254`` or an internal service.
    Loopback and everything not in the block/private sets is allowed.
    """
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise PaymentError(f"seller URL has no host: {url!r}")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 80, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise PaymentError(f"cannot resolve seller host {host!r}: {e}") from e
    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_loopback:
            continue
        if any(ip in n for n in _BLOCKED_NETS):
            raise PaymentError(f"refusing to pay seller at blocked address {ip} ({host})")
        if any(ip in n for n in _PRIVATE_NETS) and not settings.x402_allow_private_sellers:
            raise PaymentError(
                f"refusing to pay seller at private/LAN address {ip} ({host}); "
                "set X402_ALLOW_PRIVATE_SELLERS=1 if this is intentional"
            )


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


def validate_quote(reqs_dict: dict, *, price_ceiling_usdc: float) -> tuple[float, list[str]]:
    """Deterministic pre-payment checks on a seller's 402 challenge.

    Returns (price_usdc, errors). Never signs anything; the caller refuses to pay
    if ``errors`` is non-empty.
    """
    errors: list[str] = []
    net = settings.x402_network
    try:
        price = from_atomic(reqs_dict["maxAmountRequired"])
    except (KeyError, TypeError, ValueError):
        return 0.0, ["402 challenge has no valid maxAmountRequired"]

    if reqs_dict.get("scheme") != "exact":
        errors.append(f"unexpected scheme {reqs_dict.get('scheme')!r} (want 'exact')")
    challenge_net = reqs_dict.get("network")
    if challenge_net not in (net, CAIP2.get(net)):
        errors.append(f"network mismatch: challenge {challenge_net!r} != configured {net!r}")
    want_asset = USDC_ADDRESS.get(net, "").lower()
    got_asset = str(reqs_dict.get("asset", "")).lower()
    if want_asset and got_asset and got_asset != want_asset:
        errors.append(f"asset mismatch: challenge {got_asset} != USDC {want_asset} on {net}")
    if not reqs_dict.get("payTo"):
        errors.append("402 challenge has no payTo address")
    if price <= 0:
        errors.append(f"non-positive price {price}")
    if price > price_ceiling_usdc:
        errors.append(f"price {price:.2f} USDC exceeds ceiling {price_ceiling_usdc:.2f}")
    return price, errors


_ALREADY_SETTLED_HINTS = (
    "nonce", "already used", "already been used", "authorization is used",
    "authorization already", "replay", "already settled", "already redeemed",
    "duplicate authorization",
)


def _looks_already_settled(err_text: str) -> bool:
    low = (err_text or "").lower()
    return any(h in low for h in _ALREADY_SETTLED_HINTS)


def _decode_settlement(resp) -> dict:
    resp_header = resp.headers.get("x-payment-response")
    if not resp_header:
        return {}
    try:
        return json.loads(base64.b64decode(resp_header))
    except Exception:
        return {}


def _mk_payment(reqs_dict: dict, url: str, price: float, *, settlement: dict,
                request_id: str, pay_status: str, delivery_status: str, nonce: str) -> Payment:
    return Payment(
        seller=reqs_dict.get("description", url),
        resource=reqs_dict.get("resource", url),
        amount_usdc=price,
        network=reqs_dict.get("network", settings.x402_network),
        tx_hash=settlement.get("txHash") or "",
        settled=(pay_status == "settled"),
        mode="live" if settings.x402_mode == "live" else "mock",
        request_id=request_id,
        pay_status=pay_status,
        delivery_status=delivery_status,
        authorization=nonce,
    )


def paid_get(
    base_url: str,
    path: str,
    budget,
    *,
    run_id: str,
    seller_id: str,
    params: dict | None = None,
    price_ceiling_usdc: float | None = None,
) -> tuple[dict, Payment]:
    """Full 402 handshake with a persistent budget + idempotent reclaim.

    ``budget`` is an ``alphabazaar.ledger.BudgetLedger``. The call is keyed by
    ``request_id = sha256(run_id | seller_id)``:

    * a fresh call reserves budget, signs one authorization, pays, settles;
    * a repeat call (same run_id, e.g. ``--resume-run``) never signs again — it
      replays the stored authorization to reclaim the body, or reports the prior
      payment as unresolved.
    """
    from .ledger import BudgetError

    ceiling = price_ceiling_usdc if price_ceiling_usdc is not None else settings.x402_max_price_usdc
    url = base_url.rstrip("/") + path
    _guard_seller_url(url)
    request_id = budget.request_id(run_id, seller_id)
    prior = budget.get(request_id)

    # ---- reclaim path: a prior attempt already booked this request ----------
    _reclaimable = prior and (
        prior["pay_status"] in ("settled", "settlement_unknown")
        or (prior["pay_status"] == "reserved" and prior["auth_header"])
    )
    if _reclaimable:
        if not prior["auth_header"]:
            raise PaymentError(
                f"{seller_id}: prior payment {request_id[:10]}… is {prior['pay_status']} "
                "with no stored authorization — not retrying (start a new run to re-pay)"
            )
        with httpx.Client(timeout=90.0) as c:
            try:
                again = c.get(url, params=params, headers={"X-PAYMENT": prior["auth_header"]})
            except Exception as e:
                raise PaymentError(
                    f"{seller_id}: reclaim of unresolved payment {request_id[:10]}… failed: {e}"
                ) from e
        reqs_like = {"description": prior["seller_id"], "network": prior["network"], "resource": url}
        amt = round(int(prior["amount_atomic"]) / 1_000_000, 6)
        if again.status_code == 200:
            settlement = _decode_settlement(again)
            body = again.json()
            n = len(body.get("signals", []))
            budget.mark_settled(request_id, tx_hash=settlement.get("txHash", prior["tx_hash"]),
                                note="reclaimed")
            budget.mark_delivered(request_id, signals_count=n)
            return body, _mk_payment(
                reqs_like, url, amt, settlement=settlement, request_id=request_id,
                pay_status="settled", delivery_status="delivered", nonce=prior["auth_nonce"],
            )

        # A 402 whose error says the authorization was already consumed is strong
        # evidence the ORIGINAL payment settled on-chain (EIP-3009 nonces are
        # single-use). Record it as settled — the money moved — but flag that the
        # analysis body could not be recovered from the seller.
        err_text = ""
        try:
            err_text = str(again.json().get("error", ""))
        except Exception:
            err_text = again.text[:200]
        if again.status_code == 402 and _looks_already_settled(err_text):
            budget.mark_settled(request_id, tx_hash=prior["tx_hash"],
                                note=f"nonce already consumed on reclaim → original payment settled: {err_text}")
            budget.mark_delivery_failed(request_id, reason="reclaim rejected (nonce used); seller did not return the analysis")
            raise PaymentError(
                f"{seller_id}: prior payment {request_id[:10]}… is confirmed SETTLED "
                f"(authorization already consumed), but the analysis body is not recoverable "
                f"from the seller — treat as paid, not delivered ({err_text})"
            )
        raise PaymentError(
            f"{seller_id}: prior payment {request_id[:10]}… still unresolved "
            f"(reclaim got HTTP {again.status_code}: {err_text}); budget stays reserved, not re-paying"
        )
    if prior and prior["pay_status"] == "failed":
        raise PaymentError(
            f"{seller_id}: a prior attempt failed pre-settlement ({prior['settle_note']}); "
            "start a new run to retry"
        )

    # ---- fresh payment ----------------------------------------------------
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

        price, qerrors = validate_quote(reqs_dict, price_ceiling_usdc=ceiling)
        if qerrors:
            raise PaymentError(f"{seller_id}: refusing to pay — quote failed validation: "
                               + "; ".join(qerrors))

        try:
            res = budget.reserve(
                request_id=request_id, run_id=run_id, seller_id=seller_id,
                amount_usdc=price, quote_max_usdc=price,
                pay_to=reqs_dict["payTo"], asset=str(reqs_dict.get("asset", "")),
            )
        except BudgetError as e:
            raise PaymentError(f"{seller_id}: {e}") from e

        header = _build_payment_header(reqs_dict)
        try:
            nonce = json.loads(base64.b64decode(header)).get("payload", {}).get(
                "authorization", {}).get("nonce", "")
        except Exception:
            nonce = ""
        budget.attach_authorization(request_id, auth_nonce=nonce, auth_header=header)

        try:
            paid = c.get(url, params=params, headers={"X-PAYMENT": header})
        except Exception as e:
            # we sent an authorization and never heard back — do NOT assume unpaid
            budget.mark_settlement_unknown(request_id, note=f"no response after X-PAYMENT: {e}")
            raise PaymentError(
                f"{seller_id}: sent payment but got no response ({e}); marked settlement_unknown "
                f"(resume with --resume-run {run_id})"
            ) from e

        if paid.status_code == 402:
            try:
                err = paid.json().get("error", "payment rejected")
            except Exception:
                err = "payment rejected"
            budget.mark_failed(request_id, reason=f"402 after payment: {err}")
            raise PaymentError(f"{url} rejected the payment: {err}")
        if paid.status_code != 200:
            budget.mark_settlement_unknown(
                request_id, note=f"HTTP {paid.status_code} after X-PAYMENT")
            raise PaymentError(
                f"{seller_id}: HTTP {paid.status_code} after payment; marked settlement_unknown "
                f"(resume with --resume-run {run_id})"
            )

    settlement = _decode_settlement(paid)
    settled_ok = bool(settlement.get("success", settings.x402_mode == "mock"))
    result_body = paid.json()
    n_signals = len(result_body.get("signals", []))

    if settled_ok:
        budget.mark_settled(request_id, tx_hash=settlement.get("txHash", ""))
        budget.mark_delivered(request_id, signals_count=n_signals)
        pay_status = "settled"
    else:
        # got the goods but the settlement receipt says otherwise — flag, don't lie
        budget.mark_settlement_unknown(request_id, note="200 body but settlement.success falsey")
        budget.mark_delivered(request_id, signals_count=n_signals)
        pay_status = "settlement_unknown"

    payment = _mk_payment(
        reqs_dict, url, price, settlement=settlement, request_id=request_id,
        pay_status=pay_status, delivery_status="delivered", nonce=nonce,
    )
    return result_body, payment
