"""The read-only workbench renders from report JSON + the ledger, offline."""

from __future__ import annotations

import json

from alphabazaar import board
from alphabazaar.ledger import BudgetLedger


def _run_json(run_id, *, complete=True, gate=True):
    return {
        "generated_at": f"2026-09-09T0{run_id[-1]}:00:00+00:00",
        "run_id": run_id,
        "binance_mode": "snapshot",
        "x402_mode": "mock",
        "spend_usdc": 4.5,
        "budget_usdc": 20.0,
        "budget_remaining_usdc": 15.5,
        "sellers_attempted": 3,
        "sellers_delivered": 3 if complete else 1,
        "analysis_complete": complete,
        "trade_execution_allowed": gate,
        "trade_block_reasons": [] if gate else ["snapshot mode never places real orders"],
        "payments": [{"request_id": "abc", "seller": "funding", "amount_usdc": 1.5,
                      "pay_status": "settled", "tx_hash": "0x" + "a" * 64}],
        "signals": [{"kind": "risk", "asset": "SOL", "title": "conc", "detail": "d",
                     "seller_id": "risk", "request_id": "abc"}],
    }


def _ledger(tmp_path):
    led = BudgetLedger(payment_identity="0xP", network="base-sepolia",
                       daily_cap_usdc=100.0, db_path=str(tmp_path / "l.db"))
    for i, (seller, price, pay, delivered) in enumerate([
        ("funding", 1.5, "settled", True),
        ("risk", 1.75, "settled", True),
        ("momentum", 1.25, "settlement_unknown", False),
    ]):
        rid = f"k{i}"
        led.reserve(request_id=rid, run_id="run-01", seller_id=seller, amount_usdc=price,
                    quote_max_usdc=price, pay_to="0xS")
        if pay == "settled":
            led.mark_settled(rid, tx_hash="0xabc")
        else:
            led.mark_settlement_unknown(rid, note="lost")
        if delivered:
            led.mark_delivered(rid, signals_count=1)
    return led


def test_board_renders_all_sections(tmp_path, monkeypatch):
    monkeypatch.delenv("SELLER_REGISTRY", raising=False)
    reports = tmp_path / "reports"
    (reports / "runs").mkdir(parents=True)
    for rid in ("2026-09-09__run1", "2026-09-09__run2"):
        (reports / "runs" / f"{rid}.json").write_text(json.dumps(_run_json(rid)))
        (reports / "runs" / f"{rid}.html").write_text("<html>run</html>")

    ctx = board.collect(reports, ledger=_ledger(tmp_path))
    assert ctx["totals"]["runs"] == 2
    assert ctx["totals"]["payments"] == 3
    assert ctx["totals"]["unresolved"] == 1  # the settlement_unknown / undelivered momentum

    html = board.render(ctx)
    assert "Workbench" in html
    assert "Unresolved payments" in html
    assert "--resume-run run-01" in html          # reclaim hint present
    assert "Seller reputation" in html
    assert "Payment ledger" in html
    assert "runs/2026-09-09__run1.html" in html   # links to the run report
    assert "momentum" in html


def test_board_without_ledger_still_renders(tmp_path):
    reports = tmp_path / "reports"
    (reports / "runs").mkdir(parents=True)
    (reports / "runs" / "2026-09-09__x.json").write_text(json.dumps(_run_json("2026-09-09__x")))

    ctx = board.collect(reports, ledger=None)
    assert ctx["ledger_present"] is False
    assert ctx["totals"]["runs"] == 1
    html = board.render(ctx)
    assert "payment history unavailable" in html


def test_board_write_creates_file(tmp_path):
    reports = tmp_path / "reports"
    (reports / "runs").mkdir(parents=True)
    out = board.write(reports_dir=reports)
    assert out.exists() and out.name == "board.html"
