"""Access to the Binance Agentic sub-account.

Two implementations:

* ``MockBinanceClient``  — synthetic portfolio + live-or-synthetic market data.
  Runs fully offline. This is the default and what the demo video uses.

* ``McpBinanceClient``   — real Binance Agent OS MCP server (Streamable HTTP +
  OAuth), defined in ``alphabazaar/mcp_client.py``. Run ``cli.py mcp-auth`` once.
"""

from __future__ import annotations

from typing import Protocol

from . import marketdata
from .config import settings
from .models import Holding, MarketQuote, Portfolio, RebalanceAction

ASSETS = ["BTC", "ETH", "BNB", "SOL"]


class BinanceClient(Protocol):
    def get_portfolio(self) -> Portfolio: ...
    def get_market(self, assets: list[str]) -> list[MarketQuote]: ...
    def execute_convert(self, action: RebalanceAction) -> dict: ...


# --------------------------------------------------------------------------- mock
class MockBinanceClient:
    SUB_ACCOUNT = "agentic-sub-01 (mock)"

    # a deliberately lopsided book so the report has something to say
    _QTY = {"BTC": 0.07, "ETH": 0.9, "BNB": 4.0, "SOL": 12.0}
    _CASH = 20.0

    def get_market(self, assets: list[str]) -> list[MarketQuote]:
        tickers = marketdata.get_spot_tickers(assets)
        funding = marketdata.get_funding_rates(assets)
        out = []
        for a in assets:
            t = tickers[a]
            out.append(
                MarketQuote(
                    symbol=f"{a}USDT",
                    price=t["price"],
                    change_24h_pct=t["change_24h_pct"],
                    volume_24h_usdc=t["volume_24h_usdc"],
                    funding_rate_8h_pct=funding.get(a),
                )
            )
        return out

    def get_portfolio(self) -> Portfolio:
        market = {q.symbol[:-4]: q for q in self.get_market(ASSETS)}
        holdings = []
        for a, qty in self._QTY.items():
            q = market[a]
            holdings.append(
                Holding(
                    asset=a,
                    free=qty,
                    price_usdc=q.price,
                    value_usdc=round(qty * q.price, 2),
                    change_24h_pct=q.change_24h_pct,
                )
            )
        return Portfolio(sub_account=self.SUB_ACCOUNT, holdings=holdings, cash_usdc=self._CASH)

    def execute_convert(self, action: RebalanceAction) -> dict:
        return {
            "status": "FILLED",
            "sub_account": self.SUB_ACCOUNT,
            "from": action.from_asset,
            "to": action.to_asset,
            "from_qty": action.from_qty,
            "to_qty": action.est_to_qty,
            "note": "mock fill — no real order placed",
        }


# --------------------------------------------------------------------------- live
# McpBinanceClient lives in alphabazaar/mcp_client.py and is imported lazily so
# mock mode never needs the `mcp` package. See that module + `cli.py mcp-auth`.


def get_client() -> BinanceClient:
    if settings.binance_mode == "live":
        from .mcp_client import McpBinanceClient

        return McpBinanceClient()
    return MockBinanceClient()
