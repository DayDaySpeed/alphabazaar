"""Offline smoke tests — no network, no credentials."""

from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient

from alphabazaar import brain, x402
from alphabazaar.binance_client import ASSETS, MockBinanceClient
from alphabazaar.models import Payment, Report, Signal
from alphabazaar.report import render_html
from alphabazaar.x402 import Ledger
from sellers.funding_scanner import app as funding_app
from sellers.risk_analyzer import app as risk_app


def test_mock_portfolio_and_market():
    c = MockBinanceClient()
    p = c.get_portfolio()
    assert p.total_value_usdc > 0
    assert len(c.get_market(ASSETS)) == len(ASSETS)
    assert abs(sum(p.weight(h.asset) for h in p.holdings) + p.weight("USDC") - 100) < 1.0


def test_x402_handshake_funding():
    client = TestClient(funding_app)
    r1 = client.get("/analysis")
    assert r1.status_code == 402
    reqs = r1.json()["accepts"][0]
    assert reqs["scheme"] == "exact"

    header = x402._build_payment_header(reqs)
    r2 = client.get("/analysis", headers={"X-PAYMENT": header})
    assert r2.status_code == 200
    body = r2.json()
    assert body["seller"] == "funding-scanner"
    assert isinstance(body["signals"], list) and body["signals"]

    settle = json.loads(base64.b64decode(r2.headers["x-payment-response"]))
    assert settle["success"] and settle["txHash"].startswith("0x")


def test_x402_rejects_tampered_payment():
    client = TestClient(risk_app)
    reqs = client.get("/analysis").json()["accepts"][0]
    header = x402._build_payment_header(reqs)
    payload = json.loads(base64.b64decode(header))
    payload["payload"]["signature"] = "deadbeef"
    bad = base64.b64encode(json.dumps(payload).encode()).decode()
    assert client.get("/analysis", headers={"X-PAYMENT": bad}).status_code == 402


def test_ledger_enforces_cap_and_balance():
    led = Ledger(balance_usdc=3.0, daily_cap_usdc=20.0)
    ok, _ = led.can_afford(1.5)
    assert ok
    ok, reason = led.can_afford(5.0)
    assert not ok and "balance" in reason
    led2 = Ledger(balance_usdc=100.0, daily_cap_usdc=2.0)
    ok, reason = led2.can_afford(3.0)
    assert not ok and "cap" in reason


def test_planner_rules_pick_sellers():
    from alphabazaar.registry import load_sellers

    c = MockBinanceClient()
    sellers = load_sellers()
    picks, rationale = brain._plan_rules(c.get_portfolio(), c.get_market(ASSETS), sellers)
    assert "funding" in picks
    assert set(picks) <= {e["id"] for e in sellers}
    assert rationale


def test_report_renders_html():
    c = MockBinanceClient()
    p = c.get_portfolio()
    report = Report(
        sub_account=p.sub_account,
        portfolio=p,
        market=c.get_market(ASSETS),
        plan_rationale="test plan",
        payments=[
            Payment(seller="funding-scanner", resource="/analysis", amount_usdc=1.5,
                    network="base-sepolia", tx_hash="0x" + "ab" * 32, settled=True, mode="mock")
        ],
        signals=[Signal(kind="opportunity", asset="BTC", title="carry", detail="d", metrics={"apr": 22.0})],
        narrative="all good",
        rebalance=[],
        spend_usdc=1.5,
        budget_usdc=20.0,
    )
    html = render_html(report)
    assert "AlphaBazaar" in html
    assert "funding-scanner" in html
    assert "carry" in html
