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


def test_expired_or_future_authorization_is_rejected(monkeypatch):
    monkeypatch.setattr(x402.settings, "x402_mode", "mock")
    reqs = _reqs()

    def _signed(auth: dict) -> str:
        payload = {
            "x402Version": 1, "scheme": "exact", "network": "base-sepolia",
            "payload": {"authorization": auth, "signature": x402._mock_signature(auth)},
        }
        return base64.b64encode(json.dumps(payload).encode()).decode()

    base_auth = {
        "from": x402.payer_address(), "to": reqs.pay_to, "value": reqs.max_amount_required,
        "nonce": "0x" + "11" * 32,
    }
    expired = dict(base_auth, validAfter="0", validBefore="100")  # long past
    ok, reason, _ = x402.verify_payment_header(_signed(expired), reqs)
    assert not ok and "expired" in reason

    future = dict(base_auth, validAfter="99999999999", validBefore="99999999999999")
    ok, reason, _ = x402.verify_payment_header(_signed(future), reqs)
    assert not ok and "not yet valid" in reason


def _fake_dns(monkeypatch, ip):
    monkeypatch.setattr(x402.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", (ip, 80))])


def test_seller_url_guard_blocks_link_local_allows_loopback(monkeypatch):
    # loopback: always allowed
    x402._guard_seller_url("http://127.0.0.1:8801/analysis")

    # link-local / cloud metadata: always blocked
    _fake_dns(monkeypatch, "169.254.169.254")
    with pytest.raises(x402.PaymentError, match="blocked address"):
        x402._guard_seller_url("http://metadata.internal/latest")

    # classic RFC-1918 LAN: blocked unless explicitly allowed
    _fake_dns(monkeypatch, "10.0.0.5")
    with pytest.raises(x402.PaymentError, match="private/LAN address"):
        x402._guard_seller_url("http://internal-seller/analysis")
    monkeypatch.setattr(x402.settings, "x402_allow_private_sellers", True)
    x402._guard_seller_url("http://internal-seller/analysis")  # now allowed


def test_seller_url_guard_allows_vpn_sentinel_ranges(monkeypatch):
    # VPN / WARP resolvers hand back 198.18.x / 100.64.x for public hosts — must NOT block
    for ip in ("198.18.14.211", "100.64.0.7", "8.8.8.8"):
        _fake_dns(monkeypatch, ip)
        x402._guard_seller_url("https://alphabazaar-funding-scanner.onrender.com/analysis")


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
