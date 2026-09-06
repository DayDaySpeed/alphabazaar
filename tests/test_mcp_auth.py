"""OAuth configuration regressions; no credentials or network required."""

import asyncio
import json
from unittest.mock import patch

import pytest

from alphabazaar import mcp_client
from alphabazaar.binance_client import ASSETS, get_client
from alphabazaar.models import RebalanceAction


def test_binance_without_metadata_fails_before_registration(monkeypatch):
    monkeypatch.setattr(mcp_client.settings, "binance_oauth_client_metadata_url", "")

    async def connect():
        async with mcp_client._session("https://agent.binance.com/mcp/agentic"):
            pytest.fail("Session must not open without client metadata")

    with patch.object(mcp_client, "OAuthClientProvider") as provider:
        with pytest.raises(ValueError, match="BINANCE_OAUTH_CLIENT_METADATA_URL"):
            asyncio.run(connect())
        provider.assert_not_called()


def test_metadata_url_reaches_sdk_as_public_client(monkeypatch):
    url = "https://example.com/alphabazaar-oauth.json"
    monkeypatch.setattr(mcp_client.settings, "binance_oauth_client_metadata_url", url)

    class StopBeforeNetwork(Exception):
        pass

    async def connect():
        async with mcp_client._session("https://agent.binance.com/mcp/agentic"):
            pytest.fail("Network should not be reached")

    with patch.object(mcp_client, "OAuthClientProvider", side_effect=StopBeforeNetwork) as provider:
        with pytest.raises(StopBeforeNetwork):
            asyncio.run(connect())
    args = provider.call_args.kwargs
    assert args["client_metadata_url"] == url
    metadata = args["client_metadata"]
    assert metadata.token_endpoint_auth_method == "none"
    assert metadata.grant_types == ["authorization_code"]
    assert str(metadata.redirect_uris[0]) == mcp_client.REDIRECT_URI


def test_bundled_snapshot_loads_and_is_consistent(monkeypatch):
    # config.settings is a shared singleton imported by every module
    monkeypatch.setattr(mcp_client.settings, "binance_mode", "snapshot")
    monkeypatch.setattr(mcp_client.settings, "binance_snapshot", "")

    client = get_client()
    assert type(client).__name__ == "SnapshotBinanceClient"

    pf = client.get_portfolio()
    assert pf.cash_usdc > 0
    assert {h.asset for h in pf.holdings} == set(ASSETS)
    for h in pf.holdings:
        assert h.value_usdc == pytest.approx(h.free * h.price_usdc, rel=0.02)

    market = client.get_market(ASSETS)
    assert {q.symbol for q in market} == {f"{a}USDT" for a in ASSETS}

    fill = client.execute_convert(
        RebalanceAction(
            action="convert",
            from_asset="SOL",
            to_asset="USDC",
            from_qty=0.02,
            est_to_qty=2.1,
            rationale="trim",
        )
    )
    assert fill["status"] == "RECORDED"


def test_snapshot_missing_file_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_client.settings, "binance_snapshot", str(tmp_path / "nope.json"))
    with pytest.raises(FileNotFoundError, match="snapshot not found"):
        mcp_client.SnapshotBinanceClient()


def test_snapshot_schema_round_trips(tmp_path):
    payload = {
        "sub_account": "agentic (test)",
        "portfolio": {
            "cash_usdc": 5.0,
            "holdings": [
                {"asset": "BTC", "free": 0.001, "price_usdc": 60000.0, "value_usdc": 60.0}
            ],
        },
        "market": [
            {"symbol": "BTCUSDT", "price": 60000.0, "change_24h_pct": 1.2,
             "volume_24h_usdc": 1.0, "funding_rate_8h_pct": None}
        ],
    }
    p = tmp_path / "snap.json"
    p.write_text(json.dumps(payload))
    client = mcp_client.SnapshotBinanceClient(p)
    pf = client.get_portfolio()
    assert pf.sub_account == "agentic (test)"
    assert pf.total_value_usdc == pytest.approx(65.0)
    assert client.get_market(["BTC"])[0].funding_rate_8h_pct is None
