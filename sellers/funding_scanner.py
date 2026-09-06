"""Seller A — Funding-rate & basis scanner.

Sells a scan of perpetual funding rates and spot/perp basis across the majors,
flagging cash-and-carry style opportunities and funding-driven risk.
Price: $1.50 per call (x402).
"""

from __future__ import annotations

from fastapi import FastAPI, Request

from alphabazaar import marketdata
from alphabazaar.models import Signal

from .common import charge, with_settlement

PRICE_USDC = 1.50
PAY_TO = "0xF00D5canner00000000000000000000000000A1pha"
ASSETS = ["BTC", "ETH", "BNB", "SOL"]

app = FastAPI(title="AlphaBazaar Seller · Funding Scanner")


@app.get("/")
def root():
    return {
        "agent": "funding-scanner",
        "sells": "perp funding + spot/perp basis scan",
        "price_usdc": PRICE_USDC,
        "endpoint": "GET /analysis  (x402)",
    }


def _scan() -> list[Signal]:
    funding = marketdata.get_funding_rates(ASSETS)
    tickers = marketdata.get_spot_tickers(ASSETS)
    signals: list[Signal] = []

    # annualise 8h funding: 3 windows/day * 365
    for a in ASSETS:
        f8h = funding.get(a)
        if f8h is None:
            continue
        apr = f8h * 3 * 365
        px = tickers[a]["price"]
        if abs(apr) >= 15:
            side = "longs pay shorts" if apr > 0 else "shorts pay longs"
            kind = "opportunity" if apr > 0 else "risk"
            title = (
                f"{a} funding {f8h:+.3f}%/8h (~{apr:+.0f}% APR) — {side}"
            )
            detail = (
                f"Cash-and-carry: hold spot {a}, short the perp to harvest ~{abs(apr):.0f}% "
                f"annualised while delta-neutral. Spot ref ${px:,.0f}."
                if apr > 0
                else f"Negative funding on {a} signals crowded shorts / squeeze risk on any {a} perp exposure."
            )
            signals.append(
                Signal(
                    kind=kind,
                    asset=a,
                    title=title,
                    detail=detail,
                    confidence=min(0.9, 0.5 + abs(apr) / 100),
                    metrics={"funding_8h_pct": round(f8h, 4), "funding_apr_pct": round(apr, 1), "spot": round(px, 2)},
                )
            )

    if not signals:
        avg = sum(v for v in funding.values() if v is not None) / max(1, len(funding))
        signals.append(
            Signal(
                kind="info",
                asset="PORTFOLIO",
                title=f"Funding neutral across majors (avg {avg:+.3f}%/8h)",
                detail="No carry opportunity above threshold right now; perp exposure is cheap to hold either side.",
                confidence=0.7,
                metrics={"avg_funding_8h_pct": round(avg, 4)},
            )
        )
    return signals


@app.get("/analysis")
def analysis(request: Request):
    gate = charge(
        request,
        price_usdc=PRICE_USDC,
        resource=str(request.url),
        description="funding-scanner: perp funding + basis scan",
        pay_to=PAY_TO,
    )
    if gate.response is not None:
        return gate.response

    body = {
        "seller": "funding-scanner",
        "methodology": "Latest perp funding (Binance fapi premiumIndex), annualised over 3x365 windows; "
        "flags |APR| >= 15% as carry opportunity / squeeze risk.",
        "signals": [s.model_dump() for s in _scan()],
    }
    return with_settlement(body, gate.settlement)
