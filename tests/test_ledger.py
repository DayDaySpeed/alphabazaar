"""Persistent budget ledger — restart, concurrency, UTC rollover, idempotency."""

from __future__ import annotations

import threading

import pytest

from alphabazaar import ledger as ledmod
from alphabazaar.ledger import BudgetError, BudgetLedger


def _mk(path, cap=20.0, identity="0xPAYER", net="base-sepolia"):
    return BudgetLedger(payment_identity=identity, network=net, daily_cap_usdc=cap, db_path=path)


def test_atomic_units_are_exact():
    assert ledmod.usdc_to_atomic(1.75) == 1_750_000
    assert ledmod.usdc_to_atomic("0.1") + ledmod.usdc_to_atomic("0.2") == ledmod.usdc_to_atomic("0.3")
    assert ledmod.atomic_to_usdc(1_250_000) == 1.25


def test_budget_survives_restart(tmp_path):
    db = tmp_path / "l.db"
    a = _mk(db, cap=10.0)
    a.reserve(request_id="r1", run_id="run-A", seller_id="funding",
              amount_usdc=1.5, quote_max_usdc=1.5, pay_to="0xSELLER")
    a.mark_settled("r1", tx_hash="0xabc")
    assert a.remaining_usdc() == pytest.approx(8.5)

    # "restart": a brand-new ledger object on the same file
    b = _mk(db, cap=10.0)
    assert b.remaining_usdc() == pytest.approx(8.5)
    b.reserve(request_id="r2", run_id="run-B", seller_id="risk",
              amount_usdc=2.0, quote_max_usdc=2.0, pay_to="0xSELLER")
    assert b.remaining_usdc() == pytest.approx(6.5)


def test_concurrent_reservations_never_exceed_cap(tmp_path):
    db = tmp_path / "l.db"
    cap = 9.0
    price = 1.5  # 6 fit exactly
    errors = []
    oks = []

    def worker(i):
        led = _mk(db, cap=cap)
        try:
            led.reserve(request_id=f"req-{i}", run_id="race", seller_id=f"s{i}",
                        amount_usdc=price, quote_max_usdc=price, pay_to="0xSELLER")
            oks.append(i)
        except BudgetError:
            errors.append(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(oks) == 6
    assert len(errors) == 14
    final = _mk(db, cap=cap)
    assert final.spent_today_atomic() <= final.daily_cap_atomic
    assert final.spent_today_atomic() == ledmod.usdc_to_atomic(9.0)


def test_utc_day_rollover(tmp_path, monkeypatch):
    db = tmp_path / "l.db"
    monkeypatch.setattr(ledmod, "utc_day", lambda: "2026-09-09")
    d1 = _mk(db, cap=5.0)
    d1.reserve(request_id="day1", run_id="r", seller_id="funding",
               amount_usdc=4.0, quote_max_usdc=4.0, pay_to="0xS")
    assert d1.remaining_usdc() == pytest.approx(1.0)

    # next UTC day: fresh budget, yesterday's spend doesn't count
    monkeypatch.setattr(ledmod, "utc_day", lambda: "2026-09-10")
    d2 = _mk(db, cap=5.0)
    assert d2.remaining_usdc() == pytest.approx(5.0)
    d2.reserve(request_id="day2", run_id="r", seller_id="funding",
               amount_usdc=4.0, quote_max_usdc=4.0, pay_to="0xS")
    assert d2.remaining_usdc() == pytest.approx(1.0)
    # yesterday still on the books for its own date
    assert d2.spent_today_atomic(day="2026-09-09") == ledmod.usdc_to_atomic(4.0)


def test_reserve_is_idempotent_on_request_id(tmp_path):
    db = tmp_path / "l.db"
    led = _mk(db, cap=10.0)
    r1 = led.reserve(request_id="k", run_id="run", seller_id="funding",
                     amount_usdc=1.5, quote_max_usdc=1.5, pay_to="0xS")
    assert r1.resumed is False
    led.mark_settled("k", tx_hash="0xdead")

    # a repeat call (e.g. --resume-run) must NOT allocate budget again
    r2 = led.reserve(request_id="k", run_id="run", seller_id="funding",
                     amount_usdc=1.5, quote_max_usdc=1.5, pay_to="0xS")
    assert r2.resumed is True
    assert r2.pay_status == "settled"
    assert led.remaining_usdc() == pytest.approx(8.5)  # charged once, not twice


def test_failed_payment_releases_budget(tmp_path):
    db = tmp_path / "l.db"
    led = _mk(db, cap=3.0)
    led.reserve(request_id="k", run_id="r", seller_id="funding",
                amount_usdc=1.5, quote_max_usdc=1.5, pay_to="0xS")
    assert led.remaining_usdc() == pytest.approx(1.5)
    led.mark_failed("k", reason="402 after payment")
    assert led.remaining_usdc() == pytest.approx(3.0)


def test_reserve_after_failure_rechecks_budget_and_reactivates(tmp_path):
    db = tmp_path / "l.db"
    led = _mk(db, cap=2.0)
    led.reserve(request_id="k", run_id="r1", seller_id="funding",
                amount_usdc=1.5, quote_max_usdc=1.5, pay_to="0xS")
    led.mark_failed("k", reason="rejected pre-settlement")
    assert led.remaining_usdc() == pytest.approx(2.0)  # released

    # a fresh reserve on the same key must NOT silently resume the dead row —
    # it re-checks the cap and reactivates
    r = led.reserve(request_id="k", run_id="r2", seller_id="funding",
                    amount_usdc=1.5, quote_max_usdc=1.5, pay_to="0xS")
    assert r.resumed is False and r.pay_status == "reserved"
    assert led.get("k")["run_id"] == "r2"
    assert led.remaining_usdc() == pytest.approx(0.5)

    # and if the cap no longer allows it, reactivation is refused
    led.mark_failed("k", reason="rejected again")
    tight = _mk(db, cap=1.0)
    # 0 spent today now (k failed), but a 1.5 reserve exceeds a 1.0 cap
    with pytest.raises(BudgetError):
        tight.reserve(request_id="k", run_id="r3", seller_id="funding",
                      amount_usdc=1.5, quote_max_usdc=1.5, pay_to="0xS")


def test_settlement_unknown_keeps_budget_reserved(tmp_path):
    db = tmp_path / "l.db"
    led = _mk(db, cap=3.0)
    led.reserve(request_id="k", run_id="r", seller_id="funding",
                amount_usdc=1.5, quote_max_usdc=1.5, pay_to="0xS")
    led.mark_settlement_unknown("k", note="no response after X-PAYMENT")
    # we do NOT know it failed -> it still counts against the cap
    assert led.remaining_usdc() == pytest.approx(1.5)
    assert led.unresolved(run_id="r")[0]["request_id"] == "k"
