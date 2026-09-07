"""x402 v1 `exact`/EVM wire format — signing + verification, no network."""

import base64
import json

import pytest

from alphabazaar import x402


def _reqs():
    return x402.make_requirements(
        price_usdc=1.5,
        resource="http://seller.test/analysis",
        description="funding scanner",
        pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
    )


def test_payment_payload_is_v1_eip3009_shape(monkeypatch):
    monkeypatch.setattr(x402.settings, "x402_mode", "mock")
    reqs = _reqs()
    payload = json.loads(base64.b64decode(x402._build_payment_header(reqs.to_dict())))

    assert payload["x402Version"] == 1
    assert payload["scheme"] == "exact"
    assert payload["network"] == "base-sepolia"
    auth = payload["payload"]["authorization"]
    assert set(auth) == {"from", "to", "value", "validAfter", "validBefore", "nonce"}
    assert auth["to"].lower() == reqs.pay_to.lower()
    assert auth["value"] == reqs.max_amount_required  # atomic units, exact
    assert auth["nonce"].startswith("0x") and len(auth["nonce"]) == 66
    assert int(auth["validBefore"]) > int(auth["validAfter"])


def test_mock_verify_roundtrip_and_tamper(monkeypatch):
    monkeypatch.setattr(x402.settings, "x402_mode", "mock")
    reqs = _reqs()
    header = x402._build_payment_header(reqs.to_dict())

    ok, reason, payload = x402.verify_payment_header(header, reqs)
    assert ok, reason
    settle = x402.settle_payment(payload, reqs)
    assert settle["success"] and settle["txHash"].startswith("0x")

    payload["payload"]["signature"] = "0xdeadbeef"
    tampered = base64.b64encode(json.dumps(payload).encode()).decode()
    ok, reason, _ = x402.verify_payment_header(tampered, reqs)
    assert not ok and reason == "bad signature"


def test_underpay_is_rejected(monkeypatch):
    monkeypatch.setattr(x402.settings, "x402_mode", "mock")
    reqs = _reqs()
    payload = json.loads(base64.b64decode(x402._build_payment_header(reqs.to_dict())))
    payload["payload"]["authorization"]["value"] = "1"
    payload["payload"]["signature"] = x402._mock_signature(payload["payload"]["authorization"])
    header = base64.b64encode(json.dumps(payload).encode()).decode()

    ok, reason, _ = x402.verify_payment_header(header, reqs)
    assert not ok and "below maxAmountRequired" in reason


def test_live_mode_signs_real_eip712_recoverable_to_payer(monkeypatch):
    eth_account = pytest.importorskip("eth_account")
    acct = eth_account.Account.create()
    monkeypatch.setattr(x402.settings, "x402_mode", "live")
    monkeypatch.setattr(x402.settings, "x402_wallet_private_key", acct.key.hex())

    reqs = _reqs()
    payload = json.loads(base64.b64decode(x402._build_payment_header(reqs.to_dict())))
    auth = payload["payload"]["authorization"]
    sig = payload["payload"]["signature"]

    assert auth["from"] == acct.address
    assert len(bytes.fromhex(sig[2:])) == 65  # r,s,v — what the facilitator checks on-chain

    from eth_account.messages import encode_typed_data

    msg = encode_typed_data(full_message=x402._eip712_message(auth, reqs.to_dict()))
    recovered = eth_account.Account.recover_message(msg, signature=bytes.fromhex(sig[2:]))
    assert recovered == acct.address


def test_live_mode_without_key_is_explicit(monkeypatch):
    pytest.importorskip("eth_account")
    monkeypatch.setattr(x402.settings, "x402_mode", "live")
    monkeypatch.setattr(x402.settings, "x402_wallet_private_key", "")
    with pytest.raises(x402.PaymentError, match="X402_WALLET_PRIVATE_KEY"):
        x402._build_payment_header(_reqs().to_dict())
