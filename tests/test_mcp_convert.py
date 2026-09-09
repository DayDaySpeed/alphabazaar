"""The live Convert adapter: two-step flow, tool-name matching, quote binding.

No network — a fake MCP session stands in. Verifies the adapter against the
real ``convert_sendQuoteRequest`` / ``convert_acceptQuote`` / ``convert_orderStatus``
tool names and arg shapes.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from alphabazaar import mcp_client
from alphabazaar.models import RebalanceAction

# the real tool surface (names as the binance-mcp-server advertises them)
_REAL_TOOLS = [
    "spot_getAccount", "spot_ticker24hr",
    "convert_sendQuoteRequest", "convert_acceptQuote", "convert_orderStatus",
    "convert_listAllConvertPairs", "convert_queryOrderQuantityPrecisionPerAsset",
]


class _FakeToolResult:
    def __init__(self, payload):
        self.structured_content = payload
        self.content = None


class _FakeSession:
    def __init__(self, responses):
        self.responses = responses          # tool name -> payload (or callable(args)->payload)
        self.calls = []                     # [(name, args)]

    async def list_tools(self):
        tools = [types.SimpleNamespace(name=n) for n in _REAL_TOOLS]
        return types.SimpleNamespace(tools=tools)

    async def call_tool(self, name, args):
        self.calls.append((name, dict(args)))
        r = self.responses[name]
        payload = r(args) if callable(r) else r
        return _FakeToolResult(payload)


def _patch_run(monkeypatch, session):
    def _run(coro_fn):
        return asyncio.run(coro_fn(session))
    monkeypatch.setattr(mcp_client, "_run", _run)


def _action():
    return RebalanceAction(from_asset="SOL", to_asset="USDC", from_qty=0.05,
                           est_to_qty=5.0, rationale="trim")


def test_tool_keyword_matching_resolves_the_real_names():
    async def go():
        s = _FakeSession({})
        return (
            await mcp_client._find_tool(s, "convert_quote"),
            await mcp_client._find_tool(s, "convert_accept"),
            await mcp_client._find_tool(s, "convert_status"),
            await mcp_client._find_tool(s, "convert_precision"),
        )

    q, a, st, p = asyncio.run(go())
    assert q == "convert_sendQuoteRequest"
    assert a == "convert_acceptQuote"
    assert st == "convert_orderStatus"
    assert p == "convert_queryOrderQuantityPrecisionPerAsset"


def test_quote_convert_sends_numeric_amount_and_reads_quote_id(monkeypatch):
    session = _FakeSession({
        "convert_sendQuoteRequest": lambda args: {
            "quoteId": "q-123", "fromAmount": args["fromAmount"], "toAmount": 5.11,
            "ratio": 102.2, "validTimestamp": 9999999999999,
        },
    })
    _patch_run(monkeypatch, session)

    q = mcp_client.McpBinanceClient().quote_convert(_action())

    name, args = session.calls[0]
    assert name == "convert_sendQuoteRequest"
    assert args["fromAsset"] == "SOL" and args["toAsset"] == "USDC"
    assert isinstance(args["fromAmount"], (int, float)) and args["fromAmount"] == 0.05
    assert args["validTime"] == "30s"
    assert q["quote_id"] == "q-123" and q["to_qty"] == 5.11 and q["ttl_s"] > 0


def test_execute_convert_accepts_the_given_quote_then_polls_status(monkeypatch):
    session = _FakeSession({
        "convert_acceptQuote": {"orderId": "o-9", "orderStatus": "ACCEPT_SUCCESS"},
        "convert_orderStatus": {"orderStatus": "SUCCESS", "toAmount": 5.09},
    })
    _patch_run(monkeypatch, session)

    out = mcp_client.McpBinanceClient().execute_convert(_action(), quote_id="q-123")

    accept_call = next(c for c in session.calls if c[0] == "convert_acceptQuote")
    assert accept_call[1] == {"quoteId": "q-123"}           # exactly the approved id
    assert any(c[0] == "convert_orderStatus" for c in session.calls)  # resolved terminal state
    assert not any(c[0] == "convert_sendQuoteRequest" for c in session.calls)  # never re-quoted
    assert out["status"] == "SUCCESS" and out["order_id"] == "o-9"


def test_execute_convert_refuses_without_quote_id(monkeypatch):
    _patch_run(monkeypatch, _FakeSession({}))
    with pytest.raises(RuntimeError, match="approved quote_id"):
        mcp_client.McpBinanceClient().execute_convert(_action(), quote_id=None)


def test_execute_convert_maps_fail_status(monkeypatch):
    session = _FakeSession({
        "convert_acceptQuote": {"orderId": "o-1", "orderStatus": "FAIL"},
    })
    _patch_run(monkeypatch, session)
    out = mcp_client.McpBinanceClient().execute_convert(_action(), quote_id="q-1")
    assert out["status"] == "FAILED"
