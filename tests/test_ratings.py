"""Seller reputation aggregated from the payment ledger."""

from __future__ import annotations

from alphabazaar import ratings
from alphabazaar.ledger import BudgetLedger, usdc_to_atomic


def _ledger(tmp_path):
    return BudgetLedger(payment_identity="0xP", network="base-sepolia",
                        daily_cap_usdc=1000.0, db_path=str(tmp_path / "l.db"))


def _record(led, *, seller, price, pay="settled", delivered=True, degraded=False, rid=None):
    rid = rid or f"{seller}-{led.all_payments().__len__()}-{price}-{pay}"
    led.reserve(request_id=rid, run_id="r", seller_id=seller, amount_usdc=price,
                quote_max_usdc=price, pay_to="0xS")
    if pay == "settled":
        led.mark_settled(rid, tx_hash="0xabc")
    elif pay == "settlement_unknown":
        led.mark_settlement_unknown(rid, note="lost")
    elif pay == "failed":
        led.mark_failed(rid, reason="rejected")
    if delivered:
        led.mark_delivered(rid, signals_count=2)
    led.mark_provenance(rid, degraded=degraded)


def test_stats_aggregate_correctly(tmp_path):
    led = _ledger(tmp_path)
    for i in range(4):
        _record(led, seller="funding", price=1.5, rid=f"f{i}")
    _record(led, seller="funding", price=1.5, pay="settlement_unknown", delivered=False, rid="f-unk")
    _record(led, seller="risk", price=2.0, pay="failed", delivered=False, rid="r-fail")
    _record(led, seller="risk", price=2.0, degraded=True, rid="r-deg")

    stats = ratings.seller_stats(led)
    f = stats["funding"]
    assert f.calls == 5 and f.settled == 4 and f.settlement_unknown == 1
    assert f.delivery_rate == 0.8
    assert round(f.unknown_rate, 2) == 0.2
    assert f.avg_price_usdc == 1.5
    assert f.confident is True

    r = stats["risk"]
    assert r.calls == 2 and r.failed == 1 and r.degraded == 1
    assert r.degraded_rate == 0.5


def test_score_orders_reliable_above_flaky(tmp_path):
    led = _ledger(tmp_path)
    for i in range(6):
        _record(led, seller="good", price=1.0, rid=f"g{i}")
    for i in range(6):
        _record(led, seller="bad", price=1.0, pay="settlement_unknown", delivered=False, rid=f"b{i}")

    scores = ratings.seller_scores(led)
    assert scores["good"] > 0.8
    assert scores["bad"] < 0.4
    assert scores["good"] > scores["bad"]


def test_unknown_seller_gets_neutral_prior(tmp_path):
    led = _ledger(tmp_path)
    ranked = ratings.ranked(led, [{"id": "fresh", "name": "Fresh", "price_usdc": 1.0}])
    assert ranked[0]["stats"]["score"] == 0.6
    assert ranked[0]["stats"]["confident"] is False
    assert ranked[0]["stats"]["hit_rate_status"] == "none scored yet"


def test_low_confidence_score_is_shrunk_toward_prior(tmp_path):
    led = _ledger(tmp_path)
    _record(led, seller="one", price=1.0, rid="o0")  # 1 perfect call
    s = ratings.seller_stats(led)["one"]
    # perfect raw would be 1.0; with 1/3 confidence it is pulled toward 0.6
    assert 0.7 < s.score < 0.9
    assert s.confident is False
