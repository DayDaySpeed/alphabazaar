"""Build ``snapshot.json`` from raw Binance Agent OS MCP responses.

Binance gates the MCP endpoint to a fixed client allowlist, so AlphaBazaar's own
agent identity can't OAuth in. Instead: run the (read-only) MCP tools from a
whitelisted client — Claude Code, Cursor, … — collect the raw JSON, and pipe it
through this module to refresh the capture the demo replays:

    python -m alphabazaar.capture < raw.json > alphabazaar/snapshot.json

``raw.json`` is one object holding the verbatim tool results::

    {
      "sub_account": "agentic (uid 1274306951)",
      "captured_via": "Claude Code",
      "account": { ...spot_getAccount... },
      "tickers": {
        "BTCUSDT": { ...spot_ticker24hr (FULL)... },
        "ETHUSDT": { ... }, "BNBUSDT": { ... }, "SOLUSDT": { ... }
      },
      "premium_index": {                         # optional — funding-rate proxy
        "BTCUSDT": [ [openTime, open, high, low, close, ...], ... ],
        ...
      },
      "provenance": { ... }                      # optional, copied through as-is
    }

The output schema is what ``SnapshotBinanceClient`` reads. Values and 24h moves
come straight from the tool results — no hand arithmetic.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .binance_client import ASSETS

STABLES = {"USDC", "USDT", "BUSD", "FDUSD", "DAI", "TUSD"}
CASH_ASSET = "USDC"


def _num(d: dict, *keys: str, default: float = 0.0) -> float:
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return default


def _last_completed_funding_pct(klines: list[list[Any]]) -> float | None:
    """Close of the last *completed* 8h premium-index kline, as a percent."""
    if not klines:
        return None
    row = klines[-2] if len(klines) >= 2 else klines[-1]
    try:
        return round(float(row[4]) * 100, 4)
    except (IndexError, TypeError, ValueError):
        return None


def build_snapshot(raw: dict) -> dict:
    account = raw["account"]
    tickers = raw.get("tickers", {})
    premium = raw.get("premium_index", {})

    balances = account.get("balances", account) if isinstance(account, dict) else account
    by_asset = {}
    for bal in balances or []:
        asset = bal.get("asset") or bal.get("coin")
        if asset:
            by_asset[asset] = bal

    price = {}
    market = []
    for a in ASSETS:
        sym = f"{a}USDT"
        t = tickers.get(sym) or tickers.get(a) or {}
        px = _num(t, "lastPrice", "price", "c")
        price[a] = px
        market.append(
            {
                "symbol": sym,
                "price": px,
                "change_24h_pct": round(_num(t, "priceChangePercent", "P"), 4),
                "volume_24h_usdc": round(_num(t, "quoteVolume", "q"), 2),
                "funding_rate_8h_pct": _last_completed_funding_pct(premium.get(sym, [])),
            }
        )

    holdings = []
    cash = 0.0
    for asset, bal in by_asset.items():
        free = _num(bal, "free", "available", "balance")
        locked = _num(bal, "locked")
        if asset in STABLES:
            cash += free + locked
            continue
        if asset not in price or price[asset] <= 0 or free + locked <= 0:
            continue
        qty = free + locked
        holdings.append(
            {
                "asset": asset,
                "free": free,
                "locked": locked,
                "price_usdc": price[asset],
                "value_usdc": round(qty * price[asset], 2),
                "change_24h_pct": next(
                    (q["change_24h_pct"] for q in market if q["symbol"] == f"{asset}USDT"), 0.0
                ),
            }
        )
    holdings.sort(key=lambda h: ASSETS.index(h["asset"]) if h["asset"] in ASSETS else 99)

    out = {
        "captured_at": raw.get("captured_at"),
        "source": "Binance Agent OS MCP (https://agent.binance.com/mcp/agentic)",
        "captured_via": raw.get("captured_via", "whitelisted MCP client"),
        "note": raw.get(
            "note",
            "Real read of the isolated Agentic sub-account. Binance gates the MCP "
            "endpoint to a fixed client allowlist, so AlphaBazaar reads through a "
            "supported client and replays this capture.",
        ),
        "sub_account": str(raw.get("sub_account") or account.get("uid") or "agentic"),
        "portfolio": {"cash_usdc": round(cash, 2), "holdings": holdings},
        "market": market,
    }
    if "provenance" in raw:
        out["provenance"] = raw["provenance"]
    return out


def main(argv: list[str] | None = None) -> int:
    raw = json.load(sys.stdin)
    json.dump(build_snapshot(raw), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
