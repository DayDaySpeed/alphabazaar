"""The buyer agent's decision-making.

Every function has a deterministic implementation and an optional LLM upgrade
(`claude-opus-5` via the Anthropic SDK) that kicks in when ANTHROPIC_API_KEY is
set. The deterministic path keeps the demo reproducible with zero external deps.
"""

from __future__ import annotations

import json

from .config import settings
from .models import MarketQuote, Portfolio, RebalanceAction, Signal
from .ratings import LOW_SCORE_THRESHOLD
from .registry import catalog

# how the last plan()/synthesize() actually decided — surfaced in the report so
# the reader knows whether an LLM or the deterministic rules produced it.
_LAST_STATUS = "rules"


def last_status() -> str:
    return _LAST_STATUS


def _set_status(s: str) -> None:
    global _LAST_STATUS
    _LAST_STATUS = s


# --------------------------------------------------------------------------- plan
def plan(
    portfolio: Portfolio,
    market: list[MarketQuote],
    sellers: list[dict],
    *,
    budget_remaining_usdc: float | None = None,
    seller_scores: dict[str, float] | None = None,
) -> tuple[list[str], str]:
    """Pick which discovered seller agents are worth paying. Returns (ids, rationale).

    ``budget_remaining_usdc`` — if given, the plan won't select more than can be
    paid for today. ``seller_scores`` — if given (from ``ratings``), a seller with
    a confident score below the reliability bar is skipped.
    """
    if settings.has_brain:
        try:
            out = _plan_llm(portfolio, market, sellers,
                            budget_remaining_usdc=budget_remaining_usdc,
                            seller_scores=seller_scores)
            _set_status("llm")
            return out
        except Exception as e:  # pragma: no cover - network/parse issues
            _set_status(f"rules-fallback: LLM planner failed: {e}")
            return _plan_rules(portfolio, market, sellers,
                               budget_remaining_usdc=budget_remaining_usdc,
                               seller_scores=seller_scores,
                               note=f"(LLM planner failed: {e}; used rules)")
    _set_status("rules")
    return _plan_rules(portfolio, market, sellers,
                       budget_remaining_usdc=budget_remaining_usdc,
                       seller_scores=seller_scores)


def _by_tag(sellers: list[dict], *tags: str) -> str | None:
    for e in sellers:
        if any(t in e["tags"] for t in tags):
            return e["id"]
    return None


def _plan_rules(
    portfolio: Portfolio,
    market: list[MarketQuote],
    sellers: list[dict],
    *,
    budget_remaining_usdc: float | None = None,
    seller_scores: dict[str, float] | None = None,
    note: str = "",
) -> tuple[list[str], str]:
    seller_scores = seller_scores or {}
    by_id = {e["id"]: e for e in sellers}

    weights = {h.asset: portfolio.weight(h.asset) for h in portfolio.holdings}
    top_asset = max(weights, key=weights.get) if weights else None
    concentrated = bool(top_asset and weights[top_asset] >= 35)
    movers = [q for q in market if abs(q.change_24h_pct) >= 4]
    near_movers = [q for q in market if abs(q.change_24h_pct) >= 3]
    hot_funding = [q for q in market if q.funding_rate_8h_pct is not None and abs(q.funding_rate_8h_pct) >= 0.02]

    # (id, priority, why) — lower priority number = higher value, buy first
    candidates: list[tuple[str, int, str]] = []

    funding_id = _by_tag(sellers, "funding")
    if funding_id:
        if hot_funding:
            why = ("funding: " + ", ".join(
                f"{q.symbol[:-4]} {q.funding_rate_8h_pct:+.3f}%/8h" for q in hot_funding
            ) + " looks actionable")
        else:
            why = "funding: routine carry/squeeze check across majors"
        candidates.append((funding_id, 1, why))

    risk_id = _by_tag(sellers, "risk", "volatility")
    if risk_id and (concentrated or movers):
        bits = []
        if concentrated:
            bits.append(f"{top_asset} is {weights[top_asset]:.0f}% of book (concentration)")
        if movers:
            bits.append("24h moves: " + ", ".join(f"{q.symbol[:-4]} {q.change_24h_pct:+.1f}%" for q in movers))
        candidates.append((risk_id, 2, "risk: " + "; ".join(bits)))

    momentum_id = _by_tag(sellers, "momentum", "trend")
    if momentum_id and (near_movers or concentrated):
        if concentrated:
            why = f"momentum: check {top_asset}'s trend before trimming a {weights[top_asset]:.0f}% position"
        else:
            why = ("momentum: " + ", ".join(
                f"{q.symbol[:-4]} {q.change_24h_pct:+.1f}%" for q in near_movers
            ) + " — read the trend before rebalancing")
        candidates.append((momentum_id, 3, why))

    if not candidates and sellers:
        candidates.append((sellers[0]["id"], 9, "baseline scan only"))

    # de-dup by id (keep best priority), then order by value
    best: dict[str, tuple[int, str]] = {}
    for cid, pri, why in candidates:
        if cid not in best or pri < best[cid][0]:
            best[cid] = (pri, why)
    ordered = sorted(best.items(), key=lambda kv: kv[1][0])

    reasons: list[str] = []
    picks: list[str] = []
    spent = 0.0
    for cid, (_pri, why) in ordered:
        price = float((by_id.get(cid) or {}).get("price_usdc", 0) or 0)
        score = seller_scores.get(cid)
        if score is not None and score < LOW_SCORE_THRESHOLD:
            reasons.append(f"skipped {cid}: reliability {score:.2f} below {LOW_SCORE_THRESHOLD:.2f} bar")
            continue
        if budget_remaining_usdc is not None and spent + price > budget_remaining_usdc + 1e-9:
            left = max(0.0, budget_remaining_usdc - spent)
            reasons.append(f"skipped {cid} (${price:.2f}): only ${left:.2f} budget left today")
            continue
        picks.append(cid)
        spent += price
        note_score = f" [score {score:.2f}]" if score is not None else ""
        reasons.append(why + note_score)

    if not picks:
        reasons.append("nothing bought — no seller cleared the budget / reliability bar this run")

    rationale = "Agent plan — " + " | ".join(reasons)
    if note:
        rationale += f" {note}"
    return list(dict.fromkeys(picks)), rationale


def _client():
    import anthropic

    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _plan_llm(
    portfolio: Portfolio,
    market: list[MarketQuote],
    sellers: list[dict],
    *,
    budget_remaining_usdc: float | None = None,
    seller_scores: dict[str, float] | None = None,
) -> tuple[list[str], str]:
    cat = catalog(sellers)
    prices = {e["id"]: e.get("price_usdc") for e in sellers}
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
        "sellers_for_sale": cat,
        "seller_prices_usdc": prices,
        "seller_reliability_0_to_1": seller_scores or {},
        "x402_budget_remaining_usdc": (
            budget_remaining_usdc if budget_remaining_usdc is not None else settings.x402_daily_cap_usdc
        ),
    }
    prompt = (
        "You are a portfolio-analyst agent with a small x402 budget. Given the portfolio, live market "
        "data and the specialist seller agents for sale (id -> description), decide which sellers to pay. "
        "Only buy what materially helps this portfolio. Do NOT let the total price exceed "
        "x402_budget_remaining_usdc. Prefer higher seller_reliability; avoid sellers below 0.5 unless "
        "nothing else covers a real need. Respond with STRICT JSON: "
        '{"sellers": ["<id>", ...], "rationale": "<=60 words, name any seller you skipped and why"}.\n\n'
        + json.dumps(ctx, separators=(",", ":"))
    )
    resp = _client().messages.create(
        model=settings.model,
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    data = _extract_json("".join(b.text for b in resp.content if b.type == "text"))
    wanted = [s for s in data.get("sellers", []) if s in cat] or [sellers[0]["id"]]
    rationale = "Agent plan (LLM) — " + str(data.get("rationale", "")).strip()

    # deterministic safety net: never let the LLM overspend the remaining budget
    picks: list[str] = []
    dropped: list[str] = []
    spent = 0.0
    for cid in dict.fromkeys(wanted):
        price = float(prices.get(cid) or 0)
        if budget_remaining_usdc is not None and spent + price > budget_remaining_usdc + 1e-9:
            dropped.append(cid)
            continue
        picks.append(cid)
        spent += price
    if dropped:
        rationale += f" | budget guard dropped: {', '.join(dropped)}"
    return (picks or wanted[:1]), rationale


# ---------------------------------------------------------------------- synthesize
def synthesize(
    portfolio: Portfolio,
    market: list[MarketQuote],
    signals: list[Signal],
    *,
    analysis_complete: bool = True,
) -> tuple[str, list[RebalanceAction]]:
    if settings.has_brain:
        try:
            out = _synth_llm(portfolio, market, signals)
            if _LAST_STATUS == "rules":
                _set_status("llm")
            return out
        except Exception as e:  # pragma: no cover
            _set_status(f"rules-fallback: LLM synthesis failed: {e}")
    return _synth_rules(portfolio, market, signals, analysis_complete=analysis_complete)


def _synth_rules(
    portfolio: Portfolio, market: list[MarketQuote], signals: list[Signal],
    *, analysis_complete: bool = True,
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
        if not analysis_complete:
            lines.append(
                "INCOMPLETE: no specialist analysis was delivered this run — risk could NOT be "
                "assessed. This is 'not enough data', not 'no risk found'."
            )
        else:
            lines.append(
                "Specialist agents were paid and returned no high-conviction risk or opportunity "
                "signals; on the analysis purchased, the book looks balanced."
            )

    actions: list[RebalanceAction] = []
    # only propose a trim when a purchased risk signal actually supports it —
    # never claim the risk analyzer "flagged" something it wasn't asked
    concentration_flag = next(
        (s for s in risks if "concentr" in (s.title + s.detail).lower()), None
    )
    if analysis_complete and concentration_flag and top_asset and weights[top_asset] >= 40:
        h = next(x for x in portfolio.holdings if x.asset == top_asset)
        target_pct = 35.0
        excess_value = (weights[top_asset] - target_pct) / 100 * portfolio.total_value_usdc
        # size against the FREE (tradeable) balance only — locked coins can't convert
        free_value = h.free * h.price_usdc
        from_qty = round(min(excess_value, free_value * 0.5) / h.price_usdc, 6) if h.price_usdc else 0.0
        from_qty = min(from_qty, round(h.free, 6))
        if from_qty > 0:
            actions.append(
                RebalanceAction(
                    from_asset=top_asset,
                    to_asset="USDC",
                    from_qty=from_qty,
                    est_to_qty=round(from_qty * h.price_usdc, 2),
                    rationale=(
                        f"Trim {top_asset} from {weights[top_asset]:.0f}% toward {target_pct:.0f}%: "
                        f"the risk analyzer flagged concentration — \"{concentration_flag.title}\"."
                    ),
                )
            )
    if actions:
        suggestion = actions[0].rationale
    elif concentration_flag and not analysis_complete:
        suggestion = (
            f"trim of {concentration_flag.asset} WITHHELD — a concentration risk was flagged "
            "but analysis is incomplete this run; hold until a full read is available."
        )
    elif concentration_flag and top_asset and weights.get(top_asset, 0) < 40:
        suggestion = f"monitor {top_asset} concentration; below the {40}% trim trigger for now."
    else:
        suggestion = "hold; no rebalance needed."
    lines.append("Suggested action: " + suggestion)
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
                    from_asset=str(item["from_asset"]),
                    to_asset=str(item["to_asset"]),
                    from_qty=float(item["from_qty"]),
                    est_to_qty=float(item.get("est_to_qty", 0)),
                    rationale=str(item.get("rationale", "")),
                )
            )
        except Exception:
            # malformed / illegal model output (bad qty, same asset, non-numeric…)
            # — drop it; the deterministic validator would reject it anyway.
            continue
    return str(data.get("narrative", "")).strip(), actions


def _extract_json(text: str) -> dict:
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in model output: {text[:200]}")
    return json.loads(text[start : end + 1])
