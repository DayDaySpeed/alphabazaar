"""End-to-end reliability behaviours: incomplete analysis, payment reclaim,
per-run report isolation."""

from __future__ import annotations

import contextlib
import threading
import time

import httpx
import pytest
import uvicorn

from alphabazaar import analyst, x402
from alphabazaar.ledger import BudgetLedger
from alphabazaar.report import write_report


@contextlib.contextmanager
def _serve(app):
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.02)
        assert server.started, "seller did not start"
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ---- all sellers fail -> report says analysis is incomplete --------------
def test_all_sellers_failing_yields_incomplete_report(monkeypatch):
    for k in ("FUNDING", "RISK", "MOMENTUM"):
        monkeypatch.setenv(f"SELLER_{k}_URL", "http://127.0.0.1:9")  # nothing listening

    result = analyst.run()
    r = result.report
    assert r.sellers_attempted >= 1
    assert r.sellers_delivered == 0
    assert r.analysis_complete is False
    assert len(r.seller_errors) == r.sellers_attempted
    assert "INCOMPLETE" in r.narrative
    assert r.trade_execution_allowed is False
    assert any("incomplete" in why for why in r.trade_block_reasons)
    # and it must NOT fabricate a rebalance off analysis it never received
    assert r.rebalance == []


# ---- settled-but-response-lost is not paid twice ------------------------
def test_lost_response_is_reclaimed_not_repaid(tmp_path, monkeypatch):
    from sellers.funding_scanner import app as funding_app

    monkeypatch.setattr(x402.settings, "x402_mode", "mock")
    db = tmp_path / "l.db"

    class FlakyOnce(httpx.Client):
        tripped = False

        def get(self, url, **kw):
            if "X-PAYMENT" in (kw.get("headers") or {}) and not FlakyOnce.tripped:
                FlakyOnce.tripped = True
                raise httpx.ConnectError("dropped after payment", request=None)
            return super().get(url, **kw)

    monkeypatch.setattr(x402.httpx, "Client", FlakyOnce)

    def fresh_ledger():
        return BudgetLedger(payment_identity="0xPAYER", network="base-sepolia",
                            daily_cap_usdc=20.0, db_path=str(db))

    with _serve(funding_app) as base:
        # attempt 1: payment goes out, response is lost
        led1 = fresh_ledger()
        with pytest.raises(x402.PaymentError, match="settlement_unknown"):
            x402.paid_get(base, "/analysis", led1, run_id="run-1", seller_id="funding")

        rid = led1.request_id("run-1", "funding")
        row = led1.get(rid)
        assert row["pay_status"] == "settlement_unknown"
        spent_after_1 = led1.spent_today_atomic()
        assert spent_after_1 > 0  # budget stays consumed — we don't know it failed

        # attempt 2 (a "restart" + same run id): reclaim, do NOT sign a new auth
        led2 = fresh_ledger()
        body, payment = x402.paid_get(base, "/analysis", led2, run_id="run-1", seller_id="funding")
        assert body["seller"] == "funding-scanner"
        assert payment.pay_status == "settled"
        assert payment.delivery_status == "delivered"

        # exactly one reservation, budget charged once (not doubled)
        assert led2.spent_today_atomic() == spent_after_1
        assert len(led2.payments_for_run("run-1")) == 1


# ---- live reclaim: "nonce already used" == payment settled, body lost --------
def test_reclaim_treats_nonce_reuse_as_settled_not_unresolved(tmp_path):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.get("/analysis")
    def _always_nonce_used():  # the facilitator rejected the replayed authorization
        return JSONResponse(status_code=402, content={
            "error": "settlement failed: transferWithAuthorization: authorization is used"
        })

    led = BudgetLedger(payment_identity="0xP", network="base-sepolia",
                       daily_cap_usdc=20.0, db_path=str(tmp_path / "l.db"))
    rid = led.request_id("run-x", "funding")
    # pre-seed a payment we sent but never got a receipt for
    led.reserve(request_id=rid, run_id="run-x", seller_id="funding",
                amount_usdc=1.5, quote_max_usdc=1.5, pay_to="0xSELLER")
    led.attach_authorization(rid, auth_nonce="0xnonce", auth_header="c2VlZGVk")  # any non-empty
    led.mark_settlement_unknown(rid, note="no response after X-PAYMENT")

    with _serve(app) as base:
        with pytest.raises(x402.PaymentError, match="confirmed SETTLED"):
            x402.paid_get(base, "/analysis", led, run_id="run-x", seller_id="funding")

    row = led.get(rid)
    assert row["pay_status"] == "settled"           # money moved — recorded honestly
    assert row["delivery_status"] == "failed"       # but we never got the analysis
    assert led.spent_today_atomic() > 0             # still counts against budget


# ---- same-day runs keep separate records --------------------------------
def test_same_day_runs_do_not_overwrite(tmp_path):
    from alphabazaar.binance_client import MockBinanceClient

    c = MockBinanceClient()
    reports = []
    for _ in range(2):
        rr = analyst.run()
        reports.append(rr.report)

    assert reports[0].run_id != reports[1].run_id
    p0 = write_report(reports[0], out_dir=tmp_path)
    p1 = write_report(reports[1], out_dir=tmp_path)
    assert p0 != p1
    assert p0.exists() and p1.exists()
    assert p0.with_suffix(".json").exists()
    # latest-of-day pointer also written
    assert (tmp_path / f"{reports[1].generated_at[:10]}.html").exists()
