"""Seller B — Volatility & correlation risk analyzer.

Sells a risk read on a portfolio: realized vol per asset, a correlation matrix,
concentration, and a market regime call.
Price: $1.75 per call (x402). Accepts an optional ?holdings=BTC:0.04,ETH:0.9 hint.
"""

from __future__ import annotations

from fastapi import FastAPI, Request

from alphabazaar import marketdata
from alphabazaar.models import Signal

from .common import charge, with_settlement

PRICE_USDC = 1.75
PAY_TO = "0xR15Kanalyzer0000000000000000000000000A1pha"
ASSETS = ["BTC", "ETH", "BNB", "SOL"]

app = FastAPI(title="AlphaBazaar Seller · Risk Analyzer")


@app.get("/")
def root():
    return {
        "agent": "risk-analyzer",
        "sells": "realized vol, correlation matrix, regime call",
        "price_usdc": PRICE_USDC,
        "endpoint": "GET /analysis?holdings=BTC:0.04,ETH:0.9  (x402)",
    }


def _parse_holdings(raw: str | None) -> dict[str, float]:
    out: dict[str, float] = {}
    if not raw:
        return out
    for part in raw.split(","):
        if ":" in part:
            a, _, q = part.partition(":")
            try:
                out[a.strip().upper()] = float(q)
            except ValueError:
                continue
    return out


def _analyze(holdings: dict[str, float]) -> list[Signal]:
    closes = {a: marketdata.get_klines(a, "1h", 168) for a in ASSETS}
    vols = {a: marketdata.realized_vol_pct(closes[a]) for a in ASSETS}
    signals: list[Signal] = []

    hi = max(vols, key=vols.get)
    if vols[hi] >= 55:
        signals.append(
            Signal(
                kind="risk",
                asset=hi,
                title=f"{hi} realized vol elevated: {vols[hi]:.0f}% annualised (7d, 1h)",
                detail=f"Position sizing on {hi} should assume ~{vols[hi] / 19.1:.1f}% daily moves. "
                "Consider trimming or hedging if this is an oversized sleeve.",
                confidence=0.75,
                metrics={"realized_vol_pct": vols[hi]},
            )
        )

    btc_eth_corr = marketdata.correlation(closes["BTC"], closes["ETH"])
    if btc_eth_corr >= 0.8:
        signals.append(
            Signal(
                kind="risk",
                asset="PORTFOLIO",
                title=f"BTC–ETH correlation {btc_eth_corr:.2f} — little diversification",
                detail="Majors are moving together; a multi-asset book here carries single-factor (beta) risk. "
                "Cash or a low-beta sleeve is the only real diversifier this week.",
                confidence=0.7,
                metrics={"btc_eth_corr": btc_eth_corr},
            )
        )

    # concentration
    if holdings:
        prices = marketdata.get_spot_tickers(list(holdings))
        values = {a: holdings[a] * prices[a]["price"] for a in holdings}
        total = sum(values.values()) or 1.0
        top_a = max(values, key=values.get)
        top_w = 100 * values[top_a] / total
        if top_w >= 45:
            signals.append(
                Signal(
                    kind="risk",
                    asset=top_a,
                    title=f"Concentration: {top_a} is {top_w:.0f}% of risk assets",
                    detail=f"A {top_w:.0f}% weight in {top_a} dominates portfolio P&L. Rebalancing toward "
                    "target weights reduces idiosyncratic drawdown.",
                    confidence=0.8,
                    metrics={"top_weight_pct": round(top_w, 1)},
                )
            )

    avg_vol = sum(vols.values()) / len(vols)
    regime = "risk-off / high-vol" if avg_vol >= 60 else "neutral" if avg_vol >= 40 else "calm / low-vol"
    signals.append(
        Signal(
            kind="info",
            asset="PORTFOLIO",
            title=f"Market regime: {regime} (avg realized vol {avg_vol:.0f}%)",
            detail="Regime read drives whether to add, hold, or de-risk. "
            + ("Favor smaller size and more cash." if avg_vol >= 60 else "Normal sizing is reasonable."),
            confidence=0.65,
            metrics={"avg_realized_vol_pct": round(avg_vol, 1), **{f"vol_{a}": v for a, v in vols.items()}},
        )
    )
    return signals


@app.get("/analysis")
def analysis(request: Request):
    gate = charge(
        request,
        price_usdc=PRICE_USDC,
        resource=str(request.url),
        description="risk-analyzer: vol + correlation + regime",
        pay_to=PAY_TO,
    )
    if gate.response is not None:
        return gate.response

    holdings = _parse_holdings(request.query_params.get("holdings"))
    body = {
        "seller": "risk-analyzer",
        "methodology": "7d hourly closes (Binance klines); annualised realized vol, Pearson correlation on "
        "hourly returns, concentration vs equal-weight, regime from cross-asset average vol.",
        "signals": [s.model_dump() for s in _analyze(holdings)],
    }
    return with_settlement(body, gate.settlement)
