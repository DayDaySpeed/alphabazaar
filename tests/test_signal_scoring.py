"""Directional signal accuracy: recording, maturity, hit/miss, feeding the score."""

from __future__ import annotations

import pytest

from alphabazaar import ratings, signal_scoring
from alphabazaar.ledger import BudgetLedger
from alphabazaar.models import MarketQuote, Signal


def _led(tmp_path):
    return BudgetLedger(payment_identity="0xP", network="base-sepolia",
                        daily_cap_usdc=100.0, db_path=str(tmp_path / "l.db"))


# ---- pure logic ---------------------------------------------------------
def test_direction_of():
    assert signal_scoring.direction_of("opportunity") == "bullish"
    assert signal_scoring.direction_of("risk") == "bearish"
    assert signal_scoring.direction_of("info") == "neutral"


@pytest.mark.parametrize("direction,move,expected", [
    ("bullish", 3.0, "hit"),
    ("bullish", -3.0, "miss"),
    ("bullish", 0.4, "neutral"),      # below decisive threshold
    ("bearish", -3.0, "hit"),
    ("bearish", 2.0, "miss"),
    ("neutral", 9.0, "neutral"),
])
def test_classify(direction, move, expected):
    assert signal_scoring.classify(direction, move) == expected


# ---- record + score end to end ---------------------------------------
def _signals():
    return [
        Signal(kind="opportunity", asset="BTC", title="BTC uptrend", detail="d",
               seller_id="momentum", request_id="r1",
               as_of="2026-09-09T00:00:00+00:00", horizon="7d",
               valid_until="2026-09-10T00:00:00+00:00"),
        Signal(kind="risk", asset="SOL", title="SOL concentration", detail="d",
               seller_id="risk", request_id="r2",
               as_of="2026-09-09T00:00:00+00:00", horizon="1d",
               valid_until="2026-09-10T00:00:00+00:00"),
        Signal(kind="info", asset="PORTFOLIO", title="regime neutral", detail="d",
               seller_id="risk", request_id="r2"),  # not an asset -> not recorded
    ]


def _market():
    return [
        MarketQuote(symbol="BTCUSDT", price=100.0, change_24h_pct=0, volume_24h_usdc=0),
        MarketQuote(symbol="SOLUSDT", price=50.0, change_24h_pct=0, volume_24h_usdc=0),
    ]


def test_records_only_asset_signals(tmp_path):
    led = _led(tmp_path)
    n = signal_scoring.record_run_signals(led, run_id="run1", market=_market(), signals=_signals())
    assert n == 2  # PORTFOLIO info signal skipped
    rows = led.signals_for_seller("momentum")
    assert rows[0]["asset"] == "BTC" and rows[0]["ref_price"] == 100.0
    assert rows[0]["outcome"] == "pending"


def test_not_scored_before_valid_until(tmp_path):
    led = _led(tmp_path)
    signal_scoring.record_run_signals(led, run_id="run1", market=_market(), signals=_signals())
    # "now" is before the signals' valid_until
    tally = signal_scoring.score_pending(
        led, price_fn=lambda a: 999.0, now_iso="2026-09-09T12:00:00+00:00"
    )
    assert tally == {"hit": 0, "miss": 0, "neutral": 0, "unscorable": 0}
    assert led.signals_for_seller("momentum")[0]["outcome"] == "pending"


def test_scored_after_horizon_hit_and_miss(tmp_path):
    led = _led(tmp_path)
    signal_scoring.record_run_signals(led, run_id="run1", market=_market(), signals=_signals())
    # BTC 100 -> 110 (+10%): bullish opportunity -> HIT
    # SOL  50 ->  55 (+10%): bearish risk        -> MISS
    prices = {"BTC": 110.0, "SOL": 55.0}
    tally = signal_scoring.score_pending(
        led, price_fn=lambda a: prices.get(a), now_iso="2026-09-11T00:00:00+00:00"
    )
    assert tally["hit"] == 1 and tally["miss"] == 1

    btc = led.signals_for_seller("momentum")[0]
    assert btc["outcome"] == "hit" and round(btc["move_pct"], 1) == 10.0
    # scoring is idempotent — a second pass finds nothing pending
    assert signal_scoring.score_pending(led, price_fn=lambda a: prices.get(a),
                                        now_iso="2026-09-11T00:00:00+00:00") == {
        "hit": 0, "miss": 0, "neutral": 0, "unscorable": 0}


def test_missing_price_is_unscorable(tmp_path):
    led = _led(tmp_path)
    signal_scoring.record_run_signals(led, run_id="run1", market=_market(), signals=_signals())
    tally = signal_scoring.score_pending(
        led, price_fn=lambda a: None, now_iso="2026-09-11T00:00:00+00:00"
    )
    assert tally["unscorable"] == 2


def test_hit_rate_feeds_seller_score(tmp_path):
    led = _led(tmp_path)
    # give "momentum" 3 clean paid calls so it's confident
    for i in range(3):
        rid = f"m{i}"
        led.reserve(request_id=rid, run_id="r", seller_id="momentum", amount_usdc=1.25,
                    quote_max_usdc=1.25, pay_to="0xS")
        led.mark_settled(rid, tx_hash="0x")
        led.mark_delivered(rid, signals_count=1)

    base_score = ratings.seller_stats(led)["momentum"].score

    # now record + score 4 signals: 1 hit, 3 miss -> hit_rate 0.25 (drags score down)
    sigs = []
    for i, (kind, move_asset) in enumerate([("opportunity", "up"), ("risk", "up"),
                                            ("risk", "up"), ("risk", "up")]):
        sigs.append(Signal(kind=kind, asset="BTC", title=f"s{i}", detail="d",
                           seller_id="momentum", request_id="m0",
                           as_of="2026-09-09T00:00:00+00:00",
                           valid_until="2026-09-10T00:00:00+00:00"))
    # distinct titles -> distinct signal_ids
    signal_scoring.record_run_signals(
        led, run_id="run-sig",
        market=[MarketQuote(symbol="BTCUSDT", price=100.0, change_24h_pct=0, volume_24h_usdc=0)],
        signals=sigs,
    )
    signal_scoring.score_pending(led, price_fn=lambda a: 110.0,  # +10%: bullish hit, bearish miss
                                 now_iso="2026-09-11T00:00:00+00:00")

    st = ratings.seller_stats(led)["momentum"]
    assert st.hit_rate == 0.25 and st.signals_scored == 4
    assert st.score < base_score  # a poor hit-rate pulls the blended score down
