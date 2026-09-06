"""The buyer agent's decision-making.

Every function has a deterministic implementation and an optional LLM upgrade
(`claude-opus-5` via the Anthropic SDK) that kicks in when ANTHROPIC_API_KEY is
set. The deterministic path keeps the demo reproducible with zero external deps.
"""

from __future__ import annotations

import json

from .config import settings
from .models import MarketQuote, Portfolio, RebalanceAction, Signal

SELLER_CATALOG = {
    "funding": "Funding-rate & basis scanner — perp funding carry + squeeze risk ($1.50)",
    "risk": "Volatility & correlation risk analyzer — realized vol, correlation, concentration, regime ($1.75)",
}


# --------------------------------------------------------------------------- plan
def plan(portfolio: Portfolio, market: list[MarketQuote]) -> tuple[list[str], str]:
    """Decide which seller agents are worth paying. Returns (seller_keys, rationale)."""
    if settings.has_brain:
        try:
            return _plan_llm(portfolio, market)
        except Exception as e:  # pragma: no cover - network/parse issues
            return _plan_rules(portfolio, market, note=f"(LLM planner failed: {e}; used rules)")
    return _plan_rules(portfolio, market)


def _plan_rules(portfolio: Portfolio, market: list[MarketQuote], note: str = "") -> tuple[list[str], str]:
    reasons: list[str] = []
    picks: list[str] = []

    weights = {h.asset: portfolio.weight(h.asset) for h in portfolio.holdings}
    top_asset = max(weights, key=weights.get) if weights else None
    movers = [q for q in market if abs(q.change_24h_pct) >= 4]
    hot_funding = [q for q in market if q.funding_rate_8h_pct is not None and abs(q.funding_rate_8h_pct) >= 0.02]

    if hot_funding or True:  # funding scan is cheap and always informative
        picks.append("funding")
        if hot_funding:
            reasons.append(
                "funding: "
                + ", ".join(f"{q.symbol[:-4]} {q.funding_rate_8h_pct:+.3f}%/8h" for q in hot_funding)
                + " looks actionable"
            )
        else:
            reasons.append("funding: routine carry/squeeze check across majors")

    if top_asset and weights[top_asset] >= 35 or movers:
        picks.append("risk")
        bits = []
        if top_asset and weights[top_asset] >= 35:
            bits.append(f"{top_asset} is {weights[top_asset]:.0f}% of book (concentration)")
        if movers:
            bits.append("24h moves: " + ", ".join(f"{q.symbol[:-4]} {q.change_24h_pct:+.1f}%" for q in movers))
        reasons.append("risk: " + "; ".join(bits))

    if not picks:
        picks = ["funding"]
        reasons.append("baseline funding scan only")

    rationale = "Agent plan — " + " | ".join(reasons)
    if note:
        rationale += f" {note}"
    return picks, rationale


def _client():
    import anthropic

    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _plan_llm(portfolio: Portfolio, market: list[MarketQuote]) -> tuple[list[str], str]:
    ctx = {
        "portfolio": {
            "total_value_usdc": portfolio.total_value_usdc,
            "cash_usdc": portfolio.cash_usdc,
            "holdings": [
                {"asset": h.asset, "value_usdc": h.value_usdc, "weight_pct": portfolio.weight(h.asset),
                 "change_24h_pct": h.change_24h_pct}
                for h in portfolio.holdings
            ],
        },
        "market": [
            {"symbol": q.symbol, "change_24h_pct": q.change_24h_pct, "funding_8h_pct": q.funding_rate_8h_pct}
            for q in market
        ],
        "sellers_for_sale": SELLER_CATALOG,
        "x402_budget_usdc": settings.x402_daily_cap_usdc,
    }
    prompt = (
        "You are a portfolio-analyst agent with a small x402 budget. Given the portfolio, live market "
        "data and the specialist seller agents available for purchase, decide which sellers to pay. "
        "Only buy what materially helps this portfolio. Respond with STRICT JSON: "
        '{"sellers": ["funding"|"risk", ...], "rationale": "<=60 words"}.\n\n'
        + json.dumps(ctx, separators=(",", ":"))
    )
    resp = _client().messages.create(
        model=settings.model,
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    data = _extract_json("".join(b.text for b in resp.content if b.type == "text"))
    picks = [s for s in data.get("sellers", []) if s in SELLER_CATALOG] or ["funding"]
    return picks, "Agent plan (LLM) — " + str(data.get("rationale", "")).strip()


# ---------------------------------------------------------------------- synthesize
def synthesize(
    portfolio: Portfolio,
    market: list[MarketQuote],
    signals: list[Signal],
) -> tuple[str, list[RebalanceAction]]:
    if settings.has_brain:
        try:
            return _synth_llm(portfolio, market, signals)
        except Exception:  # pragma: no cover
            pass
    return _synth_rules(portfolio, market, signals)


def _synth_rules(
    portfolio: Portfolio, market: list[MarketQuote], signals: list[Signal]
) -> tuple[str, list[RebalanceAction]]:
    weights = {h.asset: portfolio.weight(h.asset) for h in portfolio.holdings}
    top_asset = max(weights, key=weights.get) if weights else None
    risks = [s for s in signals if s.kind == "risk"]
    opps = [s for s in signals if s.kind == "opportunity"]

    lines = [
        f"Portfolio value ${portfolio.total_value_usdc:,.2f} across {len(portfolio.holdings)} assets "
        f"plus ${portfolio.cash_usdc:,.2f} USDC.",
    ]
    if top_asset:
        lines.append(f"Largest sleeve: {top_asset} at {weights[top_asset]:.0f}% of the book.")
    if opps:
        lines.append(f"{len(opps)} opportunity signal(s): " + "; ".join(s.title for s in opps) + ".")
    if risks:
        lines.append(f"{len(risks)} risk signal(s): " + "; ".join(s.title for s in risks) + ".")
    if not risks and not opps:
        lines.append("No high-conviction signals purchased this run; book looks balanced.")

    actions: list[RebalanceAction] = []
    if top_asset and weights[top_asset] >= 40:
        h = next(x for x in portfolio.holdings if x.asset == top_asset)
        target_pct = 35.0
        excess_value = (weights[top_asset] - target_pct) / 100 * portfolio.total_value_usdc
        from_qty = round(min(excess_value, h.value_usdc * 0.5) / h.price_usdc, 6)
        if from_qty > 0:
            actions.append(
                RebalanceAction(
                    action="convert",
                    from_asset=top_asset,
                    to_asset="USDC",
                    from_qty=from_qty,
                    est_to_qty=round(from_qty * h.price_usdc, 2),
                    rationale=f"Trim {top_asset} from {weights[top_asset]:.0f}% toward {target_pct:.0f}% to cut "
                    "concentration risk flagged by the risk analyzer.",
                )
            )
    lines.append(
        "Suggested action: " + (actions[0].rationale if actions else "hold; no rebalance needed.")
    )
    return " ".join(lines), actions


def _synth_llm(
    portfolio: Portfolio, market: list[MarketQuote], signals: list[Signal]
) -> tuple[str, list[RebalanceAction]]:
    ctx = {
        "portfolio": {
            "total_value_usdc": portfolio.total_value_usdc,
            "cash_usdc": portfolio.cash_usdc,
            "holdings": [
                {"asset": h.asset, "qty": h.qty, "price_usdc": h.price_usdc, "value_usdc": h.value_usdc,
                 "weight_pct": portfolio.weight(h.asset), "change_24h_pct": h.change_24h_pct}
                for h in portfolio.holdings
            ],
        },
        "signals_purchased": [s.model_dump() for s in signals],
    }
    prompt = (
        "You are a portfolio-analyst agent writing a daily report for the account owner. Using the "
        "portfolio and the signals purchased from specialist agents, write a concise briefing and, if "
        "warranted, propose at most TWO conservative rebalance actions (Convert only, inside the "
        "sub-account). Respond with STRICT JSON:\n"
        '{"narrative": "<=140 words", "rebalance": [{"from_asset": "BTC", "to_asset": "USDC", '
        '"from_qty": 0.01, "est_to_qty": 640.0, "rationale": "..."}]}\n\n'
        + json.dumps(ctx, separators=(",", ":"))
    )
    resp = _client().messages.create(
        model=settings.model,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    data = _extract_json("".join(b.text for b in resp.content if b.type == "text"))
    actions: list[RebalanceAction] = []
    for item in data.get("rebalance", [])[:2]:
        try:
            actions.append(
                RebalanceAction(
                    action="convert",
                    from_asset=str(item["from_asset"]).upper(),
                    to_asset=str(item["to_asset"]).upper(),
                    from_qty=float(item["from_qty"]),
                    est_to_qty=float(item.get("est_to_qty", 0)),
                    rationale=str(item.get("rationale", "")),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return str(data.get("narrative", "")).strip(), actions


def _extract_json(text: str) -> dict:
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in model output: {text[:200]}")
    return json.loads(text[start : end + 1])
