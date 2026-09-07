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

### 1. Prepare a Binance account (for `snapshot` / `live` mode)

1. Complete KYC on Binance.
2. Open an **Agentic sub-account** from the Agent OS page.
3. Transfer in ~**30–50 USDC** plus a little BTC/ETH/BNB for the demo.

> You can skip this and run everything in **`mock` mode** — fully offline
> (synthetic portfolio, live-or-synthetic public market data). The demo video
> uses **`snapshot` mode**: real sub-account data, read through a supported MCP
> client (see step 3 and *Platform limits we hit*).

### 2. Install

```bash
git clone <this-repo> alphabazaar && cd alphabazaar
python -m venv .venv && source .venv/bin/activate     # or: uv venv
pip install -r requirements.txt
cp .env.example .env
```

### 3. Connect the real Agentic sub-account

Binance Agent OS **gates the MCP endpoint to a fixed client allowlist** —
Claude, Claude Code, Codex, ChatGPT, Cursor, VS Code. A custom OAuth client is
rejected at the consent screen with *"The AI Agent you are using is not currently
supported."* Until Binance opens client registration there are two paths:

**`snapshot` mode (recommended, what the demo uses).** Read the sub-account
through a supported client and let AlphaBazaar replay the capture:

```bash
claude mcp add binance-mcp-server --transport http https://agent.binance.com/mcp/agentic
# authorize to the Agentic sub-account, then run this loop
BINANCE_MODE=snapshot X402_MODE=mock ./run_demo.sh --no-approve
```

The run downstream is byte-for-byte a live read — same models, same report. A
real capture (uid 1274306951, built and rebalanced with 8 live Convert orders —
IDs in the file's `provenance`) ships in `alphabazaar/snapshot.json`.

To **refresh** the capture: from the supported client, call the read-only tools
(`spot_getAccount`, `spot_ticker24hr` ×4, `futures_usds_premiumIndexKlineData`),
drop the raw results into one JSON object, and pipe it through the builder — no
hand arithmetic:

```bash
python -m alphabazaar.capture < raw.json > alphabazaar/snapshot.json  # see capture.py for the input shape
```

**`live` mode (future).** Direct CIMD OAuth — code is in place for when the
allowlist opens:

1. Host `alphabazaar/oauth-client-metadata.json` at a public HTTPS URL
   (`client_id` must equal that URL; `token_endpoint_auth_method` must be `none`).
2. Set `BINANCE_OAUTH_CLIENT_METADATA_URL` in `.env`.
3. `python -m alphabazaar.cli mcp-auth` → `mcp-probe` → `BINANCE_MODE=live ./run_demo.sh`.

### 4. Configure `.env`

| Var | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | optional; enables the `claude-opus-5` planner + briefing. Without it a deterministic planner is used. |
| `BINANCE_MODE` | `mock` | `mock` \| `snapshot` \| `live` |
| `BINANCE_SNAPSHOT` | `alphabazaar/snapshot.json` | sub-account capture used by `snapshot` mode |
| `BINANCE_OAUTH_CLIENT_METADATA_URL` | — | **required for `live` OAuth** — public HTTPS CIMD document URL |
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
| **MCP** — Agentic sub-account balances, 24h tickers, perp funding, Convert | `alphabazaar/mcp_client.py` — `SnapshotBinanceClient` (replay a real capture) + `McpBinanceClient` (direct CIMD OAuth). See *Platform limits we hit*. |
| **x402** — agent-to-agent payment (`exact` scheme, USDC on Base) | `alphabazaar/x402.py`, `sellers/common.py` |
| **Agentic sub-account isolation** — no withdrawal scope, transfers stay in-account | trades are Convert-only + human-approved |
| **Public market data** — tickers / funding / klines (no auth) | `alphabazaar/marketdata.py`, used by the seller agents |

## Platform limits we hit

Two things about Agent OS as it stands today (Sept 2026), and what we did about them:

**1. The MCP endpoint is gated to a client allowlist.**
`agent.binance.com/mcp/agentic` only accepts OAuth from Claude, Claude Code,
Codex, ChatGPT, Cursor and VS Code. A custom client — even a correct one — is
turned away at the consent screen: *"The AI Agent you are using is not currently
supported. Please connect using a supported Agent to continue."* Binance
advertises CIMD (`client_id_metadata_document_supported`), the mechanism for
third-party clients to self-register, so this reads like a soft-launch gate
rather than a permanent policy.

- *What AlphaBazaar does:* the analyst reads the sub-account **through** a
  supported client (we used Claude Code) and replays that capture in `snapshot`
  mode — same models, same report, real balances. The direct-OAuth path
  (`McpBinanceClient`, CIMD, `token_endpoint_auth_method=none`) is written and
  tested, ready for when registration opens.
- *What is genuinely live:* every balance in `snapshot.json` was created and
  rebalanced by **8 real Convert orders** through the MCP `convert_*` tools
  (order IDs in the file's `provenance`, all `orderStatus: SUCCESS`), including
  the exact trim the analyst proposed in the demo.

**2. x402 settlement runs in mock mode.**
The 402 → pay → 200 handshake, the payment headers, and the spend ledger are all
real; only the on-chain USDC transfer is simulated (`X402_MODE=mock` — a
well-formed fake tx hash, ledger still decrements and the daily cap still bites).
This is a deliberate risk choice for a public demo. Going live is a wallet key
plus a facilitator — see *Going live with x402* below; the three `# HOOK` points
are marked in `x402.py`.

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
