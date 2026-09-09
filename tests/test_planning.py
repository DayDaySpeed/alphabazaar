"""Budget-aware and reputation-aware seller selection (deterministic path)."""

from __future__ import annotations

from alphabazaar import brain
from alphabazaar.binance_client import ASSETS, MockBinanceClient
from alphabazaar.registry import load_sellers


def _inputs():
    c = MockBinanceClient()
    return c.get_portfolio(), c.get_market(ASSETS), load_sellers()


def test_full_budget_buys_multiple_sellers():
    pf, mkt, sellers = _inputs()
    picks, rationale = brain._plan_rules(pf, mkt, sellers, budget_remaining_usdc=20.0)
    assert len(picks) >= 2
    assert "Agent plan" in rationale


def test_tight_budget_drops_lower_priority_sellers():
    pf, mkt, sellers = _inputs()
    # funding is $1.50 (priority 1); only it fits in $2
    picks, rationale = brain._plan_rules(pf, mkt, sellers, budget_remaining_usdc=2.0)
    prices = {e["id"]: e["price_usdc"] for e in picks and sellers}
    spent = sum(next(e["price_usdc"] for e in sellers if e["id"] == p) for p in picks)
    assert spent <= 2.0 + 1e-9
    assert "budget left" in rationale  # explains what it skipped


def test_zero_budget_buys_nothing_and_says_so():
    pf, mkt, sellers = _inputs()
    picks, rationale = brain._plan_rules(pf, mkt, sellers, budget_remaining_usdc=0.0)
    assert picks == []
    assert "nothing bought" in rationale


def test_low_reputation_seller_is_skipped_with_reason():
    pf, mkt, sellers = _inputs()
    bad = {e["id"] for e in sellers if "funding" in e["tags"]}.pop()
    scores = {bad: 0.2}  # confidently bad
    picks, rationale = brain._plan_rules(pf, mkt, sellers, seller_scores=scores)
    assert bad not in picks
    assert f"skipped {bad}: reliability 0.20" in rationale


def test_good_reputation_does_not_block():
    pf, mkt, sellers = _inputs()
    ids = [e["id"] for e in sellers]
    picks, _ = brain._plan_rules(pf, mkt, sellers, seller_scores={i: 0.9 for i in ids})
    assert picks  # nothing skipped for reputation
