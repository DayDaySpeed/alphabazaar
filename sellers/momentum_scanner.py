"""Seller C — Momentum & trend scanner.

Sells a trend read on the majors: 7d / 30d returns, price vs moving averages,
and distance from the 30d high — flagging momentum opportunities and stretched
/ mean-reversion risk.
Price: $1.25 per call (x402).
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request

from alphabazaar import marketdata
from alphabazaar.models import Signal

from .common import charge, with_settlement

PRICE_USDC = 1.25
PAY_TO = os.environ.get("SELLER_MOMENTUM_PAY_TO", "0x3Ec0M0mentum000000000000000000000000A1pha")
ASSETS = ["BTC", "ETH", "BNB", "SOL"]

app = FastAPI(title="AlphaBazaar Seller · Momentum Scanner")


@app.get("/")
def root():
    return {
        "agent": "momentum-scanner",
        "sells": "7d/30d returns, MA crossovers, distance from the 30d high",
        "price_usdc": PRICE_USDC,
        "endpoint": "GET /analysis  (x402)",
    }


def _sma(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _scan() -> list[Signal]:
    signals: list[Signal] = []
    for a in ASSETS:
        closes = marketdata.get_klines(a, interval="1d", limit=30)
        if len(closes) < 20:
            continue
        px = closes[-1]
        ret_7d = (px / closes[-8] - 1) * 100 if len(closes) >= 8 else 0.0
        ret_30d = (px / closes[0] - 1) * 100
        sma7, sma30 = _sma(closes[-7:]), _sma(closes)
        hi30 = max(closes)
        from_high = (px / hi30 - 1) * 100  # <= 0

        trend_up = px > sma7 > sma30
        trend_dn = px < sma7 < sma30
        metrics = {
            "ret_7d_pct": round(ret_7d, 1),
            "ret_30d_pct": round(ret_30d, 1),
            "px_vs_sma7_pct": round((px / sma7 - 1) * 100, 1) if sma7 else 0.0,
            "from_30d_high_pct": round(from_high, 1),
        }

        if trend_up and ret_7d >= 3:
            signals.append(
                Signal(
                    kind="opportunity",
                    asset=a,
                    title=f"{a} in an uptrend (+{ret_7d:.1f}% 7d, price > 7d MA > 30d MA)",
                    detail=f"{a} is {from_high:+.1f}% from its 30d high with MAs stacked bullish. "
                    "Momentum favors holding / adding on pullbacks to the 7d MA.",
                    confidence=min(0.85, 0.55 + ret_7d / 40),
                    metrics=metrics,
                )
            )
        elif trend_dn and ret_7d <= -3:
            signals.append(
                Signal(
                    kind="risk",
                    asset=a,
                    title=f"{a} in a downtrend ({ret_7d:.1f}% 7d, price < 7d MA < 30d MA)",
                    detail=f"{a} MAs are stacked bearish and it sits {from_high:.1f}% below the 30d high. "
                    "Trend-following says reduce or wait for a reclaim of the 7d MA.",
                    confidence=min(0.85, 0.55 + abs(ret_7d) / 40),
                    metrics=metrics,
                )
            )
        elif ret_30d >= 25 and px < sma7:
            signals.append(
                Signal(
                    kind="risk",
                    asset=a,
                    title=f"{a} extended (+{ret_30d:.0f}% 30d) and rolling over below the 7d MA",
                    detail=f"Strong 30d run but losing short-term momentum — mean-reversion risk. "
                    f"Now {from_high:.1f}% off the high.",
                    confidence=0.6,
                    metrics=metrics,
                )
            )

    if not signals:
        t = marketdata.get_spot_tickers(ASSETS)
        best = max(ASSETS, key=lambda a: t[a]["change_24h_pct"])
        signals.append(
            Signal(
                kind="info",
                asset="PORTFOLIO",
                title="No decisive trend across the majors right now",
                detail=f"MAs are tangled; nothing is trending cleanly. {best} has the best 24h move.",
                confidence=0.6,
            )
        )
    return signals


@app.get("/analysis")
def analysis(request: Request):
    gate = charge(
        request,
        price_usdc=PRICE_USDC,
        resource=str(request.url),
        description="momentum-scanner: 7d/30d trend + MA + distance-from-high",
        pay_to=PAY_TO,
    )
    if gate.response is not None:
        return gate.response

    body = {
        "seller": "momentum-scanner",
        "methodology": "30 daily closes (Binance klines); 7d/30d returns, 7d vs 30d SMA stack, "
        "distance from the 30d high. Flags stacked-MA trends and extended-then-rolling-over setups.",
        "signals": [s.model_dump() for s in _scan()],
    }
    return with_settlement(body, gate.settlement)
