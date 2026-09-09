"""Deterministic trade validation, quote binding, and the execution gate."""

from __future__ import annotations

import time

import pytest

from alphabazaar import execution, x402
from alphabazaar.binance_client import MockBinanceClient
from alphabazaar.execution import ExecutionError, Quote
from alphabazaar.ledger import BudgetLedger
from alphabazaar.models import Portfolio, Holding, RebalanceAction
from alphabazaar.validate import validate_rebalance


def _pf():
    return Portfolio(
        sub_account="test",
        cash_usdc=5.0,
        holdings=[
            Holding(asset="SOL", free=1.0, locked=0.5, price_usdc=100.0, value_usdc=150.0),
            Holding(asset="BTC", free=0.001, locked=0.0, price_usdc=60000.0, value_usdc=60.0),
        ],
    )


# ---- model-level rejection of illegal LLM output -------------------------
@pytest.mark.parametrize("bad", [
    {"from_asset": "SOL", "to_asset": "USDC", "from_qty": 0, "est_to_qty": 1, "rationale": "x"},
    {"from_asset": "SOL", "to_asset": "USDC", "from_qty": -1, "est_to_qty": 1, "rationale": "x"},
    {"from_asset": "SOL", "to_asset": "USDC", "from_qty": float("inf"), "est_to_qty": 1, "rationale": "x"},
    {"from_asset": "SOL", "to_asset": "USDC", "from_qty": float("nan"), "est_to_qty": 1, "rationale": "x"},
    {"from_asset": "SOL", "to_asset": "SOL", "from_qty": 1, "est_to_qty": 1, "rationale": "x"},
])
def test_rebalance_action_rejects_illegal_values(bad):
    with pytest.raises(Exception):
        RebalanceAction(**bad)


# ---- validator uses FREE balance, not free+locked -----------------------
def test_validator_rejects_qty_above_free_balance():
    act = RebalanceAction(from_asset="SOL", to_asset="USDC", from_qty=1.4, est_to_qty=140,
                          rationale="trim")  # free is 1.0, locked 0.5
    v = validate_rebalance(act, _pf(), min_notional_usdc=1.0)
    assert not v.ok
    assert any("FREE balance" in e for e in v.errors)
    assert v.action.status == "rejected"


def test_validator_accepts_sane_action():
    act = RebalanceAction(from_asset="SOL", to_asset="USDC", from_qty=0.3, est_to_qty=30,
                          rationale="trim")
    v = validate_rebalance(act, _pf(), min_notional_usdc=1.0)
    assert v.ok and v.action.status == "validated"
    assert v.notional_usdc == pytest.approx(30.0)


def test_validator_rejects_unheld_asset_and_dust():
    v = validate_rebalance(
        RebalanceAction(from_asset="DOGE", to_asset="USDC", from_qty=1, est_to_qty=1, rationale="x"),
        _pf(),
    )
    assert not v.ok and any("not held" in e for e in v.errors)


# ---- quote validation (abnormal seller quotes) --------------------------
def _reqs_dict(**over):
    d = x402.make_requirements(price_usdc=1.5, resource="http://s/analysis",
                               description="d", pay_to="0xSELLER").to_dict()
    d.update(over)
    return d


def test_validate_quote_flags_overpriced_and_wrong_network(monkeypatch):
    monkeypatch.setattr(x402.settings, "x402_network", "base-sepolia")
    price, errs = x402.validate_quote(_reqs_dict(maxAmountRequired="9000000"),
                                     price_ceiling_usdc=5.0)
    assert price == 9.0 and any("ceiling" in e for e in errs)

    _, errs2 = x402.validate_quote(_reqs_dict(network="ethereum"), price_ceiling_usdc=5.0)
    assert any("network mismatch" in e for e in errs2)

    _, errs3 = x402.validate_quote(_reqs_dict(payTo=""), price_ceiling_usdc=5.0)
    assert any("payTo" in e for e in errs3)

    _, errs4 = x402.validate_quote(_reqs_dict(scheme="loose"), price_ceiling_usdc=5.0)
    assert any("scheme" in e for e in errs4)


def test_validate_quote_ok_for_clean_challenge(monkeypatch):
    monkeypatch.setattr(x402.settings, "x402_network", "base-sepolia")
    price, errs = x402.validate_quote(_reqs_dict(), price_ceiling_usdc=5.0)
    assert price == 1.5 and errs == []


# ---- execution gate ---------------------------------------------------
def _quote(action, ttl=30, verified=False):
    now = time.time()
    return Quote(
        from_asset=action.from_asset, to_asset=action.to_asset,
        from_qty=action.from_qty, to_qty=action.est_to_qty,
        rate=action.est_to_qty / action.from_qty, quote_id="q1",
        created_at=now, expires_at=now + ttl, verified=verified, source="test",
    )


def _led():
    return BudgetLedger(payment_identity="0xP", network="base-sepolia",
                        daily_cap_usdc=20.0, db_path=":memory:")


def test_live_execution_blocked_on_degraded_or_stale_data():
    client = MockBinanceClient()
    act = RebalanceAction(from_asset="SOL", to_asset="USDC", from_qty=0.3, est_to_qty=30, rationale="x")
    q = _quote(act, verified=True)
    tok = execution.approval_token("acct", act, q)
    with pytest.raises(ExecutionError, match="degraded"):
        execution.execute(client, act, q, tok, account="acct", run_id="r", ledger=_led(),
                          binance_mode="live", data_degraded=True)
    with pytest.raises(ExecutionError, match="stale"):
        execution.execute(client, act, q, tok, account="acct", run_id="r", ledger=_led(),
                          binance_mode="live", data_stale=True)


def test_live_execution_blocked_without_verified_quote():
    client = MockBinanceClient()
    act = RebalanceAction(from_asset="SOL", to_asset="USDC", from_qty=0.3, est_to_qty=30, rationale="x")
    q = _quote(act, verified=False)
    tok = execution.approval_token("acct", act, q)
    with pytest.raises(ExecutionError, match="verified"):
        execution.execute(client, act, q, tok, account="acct", run_id="r", ledger=_led(),
                          binance_mode="live")


def test_expired_quote_is_refused():
    client = MockBinanceClient()
    act = RebalanceAction(from_asset="SOL", to_asset="USDC", from_qty=0.3, est_to_qty=30, rationale="x")
    q = _quote(act, ttl=-1)
    tok = execution.approval_token("acct", act, q)
    with pytest.raises(ExecutionError, match="expired"):
        execution.execute(client, act, q, tok, account="acct", run_id="r", ledger=_led(),
                          binance_mode="mock")


def test_approval_token_is_single_use():
    client = MockBinanceClient()
    led = _led()
    act = RebalanceAction(from_asset="SOL", to_asset="USDC", from_qty=0.3, est_to_qty=30, rationale="x")
    q = _quote(act)
    tok = execution.approval_token("acct", act, q)
    r1 = execution.execute(client, act, q, tok, account="acct", run_id="r", ledger=led,
                           binance_mode="mock")
    assert r1["status"] == "FILLED"
    with pytest.raises(ExecutionError, match="already used"):
        execution.execute(client, act, q, tok, account="acct", run_id="r", ledger=led,
                          binance_mode="mock")


def test_approval_token_does_not_match_a_changed_quote():
    client = MockBinanceClient()
    act = RebalanceAction(from_asset="SOL", to_asset="USDC", from_qty=0.3, est_to_qty=30, rationale="x")
    q1 = _quote(act)
    tok = execution.approval_token("acct", act, q1)
    q2 = _quote(act)  # different quote_id timing -> different hash
    q2.quote_id = "q-DIFFERENT"
    with pytest.raises(ExecutionError, match="re-approve"):
        execution.execute(client, act, q2, tok, account="acct", run_id="r", ledger=_led(),
                          binance_mode="mock")


class _FakeVenueClient:
    """A live-ish client: each quote_convert() mints a new id; execute records what it got."""

    def __init__(self):
        self.n = 0
        self.executed_quote_id = None

    def quote_convert(self, action):
        self.n += 1
        return {"quote_id": f"venue-{self.n}", "to_qty": float(action.est_to_qty),
                "from_qty": float(action.from_qty), "ttl_s": 30}

    def execute_convert(self, action, *, quote_id=None):
        self.executed_quote_id = quote_id
        return {"status": "SUCCESS", "from": action.from_asset, "to": action.to_asset}


def test_execute_uses_the_approved_quote_id_not_a_fresh_one():
    client = _FakeVenueClient()
    act = RebalanceAction(from_asset="SOL", to_asset="USDC", from_qty=0.3, est_to_qty=30, rationale="x")

    quote = execution.get_quote(client, act)      # -> venue-1, verified
    assert quote.verified and quote.quote_id == "venue-1"
    tok = execution.approval_token("acct", act, quote)

    execution.execute(client, act, quote, tok, account="acct", run_id="r", ledger=_led(),
                      binance_mode="live")
    # the venue was NOT asked for a second quote; the approved id was executed
    assert client.n == 1
    assert client.executed_quote_id == "venue-1"


def test_verified_quote_requires_a_real_venue_quote_id():
    class NoIdClient:
        def quote_convert(self, action):
            return {"quote_id": "", "to_qty": 30.0, "from_qty": 0.3}

    act = RebalanceAction(from_asset="SOL", to_asset="USDC", from_qty=0.3, est_to_qty=30, rationale="x")
    q = execution.get_quote(NoIdClient(), act)
    assert q.verified is False  # no id -> cannot be trusted as a venue quote
