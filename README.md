# AlphaBazaar 🏛️

**An x402-powered agent-to-agent research marketplace, built on Binance Agent OS.**

Your portfolio-analyst agent connects to your Binance **Agentic sub-account**, reads
your holdings and live market data, decides which specialist analyses it needs, then
**autonomously pays other AI agents via x402** (~$1–3 each) to buy that analysis — and
hands you an actionable daily report. All trades are human-approved and stay inside an
isolated sub-account with **no withdrawal permissions**.

Submitted to the **Binance Agent OS mini hackathon** · Track: *Payment workflow (agent-to-agent payments)*.

```
┌───────────────────────────┐   x402: 402 → pay USDC → 200   ┌──────────────────────────────┐
│  Analyst agent (buyer)    │ ───────────────────────────────▶ │ Seller A · Funding scanner   │  $1.50
│  · Binance MCP client     │                                 └──────────────────────────────┘
│  · reads sub-account      │ ───────────────────────────────▶ ┌──────────────────────────────┐
│  · plans what to buy      │                                 │ Seller B · Risk analyzer     │  $1.75
│  · settles via x402       │                                 └──────────────────────────────┘
│  · writes HTML report     │
│  · human-approved Convert │
└───────────────────────────┘
```

---

## Quickstart

### 1. Prepare a Binance account (for `live` mode)

1. Complete KYC on Binance.
2. Open an **Agentic sub-account** from the Agent OS page.
3. Transfer in ~**30–50 USDC** plus a little BTC/ETH/BNB for the demo.

> You can skip this and run everything in **`mock` mode** first — it's fully offline
> (synthetic portfolio, live-or-synthetic public market data) and is what the demo
> video uses.

### 2. Install

```bash
git clone <this-repo> alphabazaar && cd alphabazaar
python -m venv .venv && source .venv/bin/activate     # or: uv venv
pip install -r requirements.txt
cp .env.example .env
```

### 3. Connect the Binance MCP server (live mode only)

```bash
claude mcp add binance-mcp-server --transport http https://agent.binance.com/mcp/agentic
# complete the OAuth consent screen — it authorizes the isolated Agentic sub-account
```

Then wire the transport in `alphabazaar/binance_client.py::McpBinanceClient` (the tool
names are stubbed to the documented surface: `get_account`, `get_ticker`,
`get_funding_rate`, `convert`) and set `BINANCE_MODE=live` in `.env`.

### 4. Configure `.env`

| Var | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | optional; enables the `claude-opus-5` planner + briefing. Without it a deterministic planner is used. |
| `BINANCE_MODE` | `mock` | `mock` \| `live` |
| `X402_MODE` | `mock` | `mock` = simulated settlement (fake tx hash), ledger still moves. `live` = real Base settlement via a facilitator. |
| `X402_DAILY_CAP_USDC` | `20` | mirrors Binance x402's $20/day cap; the agent stops buying at this. |
| `SELLER_FUNDING_URL` / `SELLER_RISK_URL` | localhost | point at deployed seller URLs when hosted |

### 5. Run the whole loop

```bash
./run_demo.sh          # starts both seller agents, runs the analyst, tears down
# or manually:
python -m uvicorn sellers.funding_scanner:app --port 8801 &
python -m uvicorn sellers.risk_analyzer:app  --port 8802 &
python -m alphabazaar.cli run
```

The analyst reads the sub-account → plans → pays each seller over x402 → writes
`reports/YYYY-MM-DD.html` → prints any proposed rebalance and asks for approval
(`y` executes a Convert **inside the sub-account**).

### 6. Tests

```bash
pytest -q      # offline; no credentials needed
```

---

## How it uses Binance Agent OS

| Agent OS capability | Where |
|---|---|
| **MCP** — Agentic sub-account balances, 24h tickers, perp funding, Convert | `alphabazaar/binance_client.py` (`McpBinanceClient`) |
| **x402** — agent-to-agent payment (`exact` scheme, USDC on Base) | `alphabazaar/x402.py`, `sellers/common.py` |
| **Agentic sub-account isolation** — no withdrawal scope, transfers stay in-account | trades are Convert-only + human-approved |
| **Public market data** — tickers / funding / klines (no auth) | `alphabazaar/marketdata.py`, used by the seller agents |

## Layout

```
alphabazaar/
  config.py         env-driven settings
  models.py         shared pydantic models
  marketdata.py     public Binance REST + offline synthetic fallback
  binance_client.py Mock + MCP sub-account clients
  x402.py           x402 handshake (client + server) + spend ledger
  brain.py          planner + report synthesis (deterministic / claude-opus-5)
  analyst.py        the buyer agent orchestration
  report.py         Report -> HTML
  cli.py            `python -m alphabazaar.cli run`
sellers/
  common.py         x402 paywall for FastAPI
  funding_scanner.py  Seller A
  risk_analyzer.py    Seller B
run_demo.sh         one-command demo
```

## Going live with x402

`x402.py` has `# HOOK` markers for the three real-settlement points:

1. `_build_payment_header` — sign an EIP-3009 `transferWithAuthorization` with `X402_WALLET_PRIVATE_KEY`.
2. `verify_payment_header` → `_facilitator_verify` — `POST {facilitator}/verify`.
3. `settle_payment` → `_facilitator_settle` — `POST {facilitator}/settle`.

Set `X402_MODE=live`, `X402_NETWORK=base`, and fund the Agentic wallet.

---

*Not financial advice. Demo software.*
