"""Signals carry enough to trace a claim back to who made it, on what, until when."""

from __future__ import annotations

import contextlib
import threading
import time

import uvicorn
from fastapi.testclient import TestClient

from alphabazaar import analyst, x402
from alphabazaar.models import Signal
from sellers.risk_analyzer import app as risk_app


def test_seller_stamps_traceability_on_every_signal():
    client = TestClient(risk_app)
    reqs = client.get("/analysis").json()["accepts"][0]
    header = x402._build_payment_header(reqs)
    body = client.get("/analysis", headers={"X-PAYMENT": header}).json()

    assert body["horizon"] and body["valid_until"]
    assert body["signals"], "expected at least one signal"
    for raw in body["signals"]:
        s = Signal(**raw)
        assert s.seller_id == "risk-analyzer"
        assert s.as_of and s.method
        assert s.horizon == body["horizon"]
        assert s.valid_until == body["valid_until"]
        assert "metrics" in s.evidence and "data_window" in s.evidence


@contextlib.contextmanager
def _serve(app):
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(cfg)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.02)
        assert server.started
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    finally:
        server.should_exit = True
        th.join(timeout=5)


def test_analyst_links_each_signal_to_the_payment_that_bought_it(monkeypatch):
    from sellers.funding_scanner import app as funding_app

    with _serve(funding_app) as base:
        for k in ("FUNDING", "RISK", "MOMENTUM"):
            monkeypatch.setenv(f"SELLER_{k}_URL", base)  # all three point at the one live seller

        result = analyst.run()

    r = result.report
    assert r.signals, "expected signals"
    by_req = {p.request_id: p for p in r.payments}
    for s in r.signals:
        assert s.seller_id
        assert s.request_id in by_req, "signal not linked to a real payment"
        assert by_req[s.request_id].amount_usdc > 0
        assert s.evidence  # never empty after analyst processing
