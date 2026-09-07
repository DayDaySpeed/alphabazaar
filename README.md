# AlphaBazaar 🏛️

**由 x402 驱动的 agent 对 agent 投研市场，构建于 Binance Agent OS。**
**An x402-powered agent-to-agent research marketplace, built on Binance Agent OS.**

中文（下方） · [English](#english)

---

## 中文

你的组合分析师 Agent 连上你的币安 **Agentic 子账户**，读取持仓和实时行情，自己判断需要哪些
专业分析，然后**通过 x402 自主付费给其他 AI Agent**（每笔约 $1–3）购买这些分析——最后交给你
一份可执行的日报。所有交易都需人工批准，且全部发生在一个**无提现权限**的隔离子账户内。

参赛 **Binance Agent OS 迷你黑客松** · 赛道：*支付工作流（Agent 与 Agent 之间的支付）*。

```
┌───────────────────────────┐   x402: 402 → 付 USDC → 200   ┌──────────────────────────────┐
│  分析师 Agent（买方）      │ ───────────────────────────────▶ │ 卖方 A · 资金费率扫描器       │  $1.50
│  · Binance MCP 客户端      │                                 └──────────────────────────────┘
│  · 读子账户                │ ───────────────────────────────▶ ┌──────────────────────────────┐
│  · 规划买什么              │                                 │ 卖方 B · 风险分析器           │  $1.75
│  · 用 x402 结算            │                                 └──────────────────────────────┘
│  · 生成 HTML 报告          │
│  · 人工批准的 Convert      │
└───────────────────────────┘
```

### 快速开始

#### 1. 准备币安账户（`snapshot` / `live` 模式需要）

1. 在币安完成 KYC。
2. 在 Agent OS 页面开启一个 **Agentic 子账户**。
3. 划入约 **30–50 USDC** 外加少量 BTC/ETH/BNB 用于演示。

> 也可以跳过这步，全程用 **`mock` 模式** ——完全离线（合成持仓、在线或合成的公开行情）。
> 演示视频用的是 **`snapshot` 模式**：真实子账户数据，经受支持的 MCP 客户端读取
> （见第 3 步和《我们遇到的平台限制》）。

#### 2. 安装

```bash
git clone <this-repo> alphabazaar && cd alphabazaar
python -m venv .venv && source .venv/bin/activate     # 或：uv venv
pip install -r requirements.txt
cp .env.example .env
```

#### 3. 连接真实 Agentic 子账户

Binance Agent OS **把 MCP 端点限制给一个固定的客户端白名单**——Claude、Claude Code、Codex、
ChatGPT、Cursor、VS Code。自定义 OAuth 客户端（即使配置完全正确）会在授权页被拒：
*"The AI Agent you are using is not currently supported."* 在币安开放客户端注册之前，有两条路：

**`snapshot` 模式（推荐，演示用的就是这个）。** 用受支持的客户端读子账户，让 AlphaBazaar 回放这份快照：

```bash
claude mcp add binance-mcp-server --transport http https://agent.binance.com/mcp/agentic
# 授权到 Agentic 子账户，然后跑这个循环
BINANCE_MODE=snapshot X402_MODE=mock ./run_demo.sh --no-approve
```

下游逻辑与 live 读取逐字节一致——同样的模型、同样的报告。仓库自带一份真实快照
（子账户 uid 1274306951，由 **8 笔真实 Convert 订单**铺仓 + 再平衡而成，order ID 在文件的
`provenance` 字段里），路径 `alphabazaar/snapshot.json`。

**刷新**快照：在受支持的客户端里调只读工具（`spot_getAccount`、`spot_ticker24hr` ×4、
`futures_usds_premiumIndexKlineData`），把原始结果拼进一个 JSON 对象，管道给构建器——不用手算：

```bash
python -m alphabazaar.capture < raw.json > alphabazaar/snapshot.json  # 输入格式见 capture.py
```

**`live` 模式（未来）。** 直连 CIMD OAuth——代码已就绪，等白名单开放：

1. 把 `alphabazaar/oauth-client-metadata.json` 托管在一个公网 HTTPS URL
   （`client_id` 必须等于该 URL；`token_endpoint_auth_method` 必须是 `none`）。
2. 在 `.env` 里设 `BINANCE_OAUTH_CLIENT_METADATA_URL`。
3. `python -m alphabazaar.cli mcp-auth` → `mcp-probe` → `BINANCE_MODE=live ./run_demo.sh`。

#### 4. 配置 `.env`

| 变量 | 默认 | 说明 |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | 可选；启用 `claude-opus-5` 规划器 + 简报。不填则用确定性规划器。 |
| `BINANCE_MODE` | `mock` | `mock` \| `snapshot` \| `live` |
| `BINANCE_SNAPSHOT` | `alphabazaar/snapshot.json` | `snapshot` 模式读取的子账户快照 |
| `BINANCE_OAUTH_CLIENT_METADATA_URL` | — | **`live` OAuth 必需**——公网 HTTPS CIMD 文档 URL |
| `X402_MODE` | `mock` | `mock` = 模拟结算（假 tx hash），账本仍递减。`live` = 经 facilitator 在 Base 上真实结算。 |
| `X402_DAILY_CAP_USDC` | `20` | 对齐币安 x402 的 $20/天上限；agent 到此停止购买。 |
| `SELLER_FUNDING_URL` / `SELLER_RISK_URL` | localhost | 卖方部署后填公开 URL |

#### 5. 跑完整循环

```bash
./run_demo.sh          # 启动两个卖方 agent，跑分析师，然后收尾
# 或手动：
python -m uvicorn sellers.funding_scanner:app --port 8801 &
python -m uvicorn sellers.risk_analyzer:app  --port 8802 &
python -m alphabazaar.cli run
```

分析师读子账户 → 规划 → 用 x402 付每个卖方 → 写 `reports/YYYY-MM-DD.html` → 打印再平衡建议
并请求批准（输 `y` 在**子账户内**执行一笔 Convert）。

#### 6. 测试

```bash
pytest -q      # 离线；无需凭证
```

### 如何用到 Binance Agent OS

| Agent OS 能力 | 用在哪 |
|---|---|
| **MCP** —— Agentic 子账户余额、24h 行情、永续资金费率、Convert | `alphabazaar/mcp_client.py` —— `SnapshotBinanceClient`（回放真实快照）+ `McpBinanceClient`（直连 CIMD OAuth）。见《我们遇到的平台限制》。 |
| **x402** —— agent 对 agent 支付（`exact` scheme，Base 上的 USDC） | `alphabazaar/x402.py`、`sellers/common.py` |
| **Agentic 子账户隔离** —— 无提现范围，划转不出账户 | 交易仅限 Convert + 人工批准 |
| **公开行情** —— tickers / 资金费率 / K 线（无需鉴权） | `alphabazaar/marketdata.py`，卖方 agent 使用 |

### 我们遇到的平台限制

关于 Agent OS 目前（2026 年 9 月）的两点，以及我们的应对：

**1. MCP 端点限白名单客户端。**
`agent.binance.com/mcp/agentic` 只接受来自 Claude、Claude Code、Codex、ChatGPT、Cursor、
VS Code 的 OAuth。自定义客户端——即使完全正确——会在授权页被拒：*"The AI Agent you are using
is not currently supported. Please connect using a supported Agent to continue."* 币安公开宣传
了 CIMD（`client_id_metadata_document_supported`），正是第三方客户端自助注册的机制，所以这
更像软启动的门槛，而非永久政策。

- *AlphaBazaar 的做法：* 分析师**经由**一个受支持的客户端（我们用 Claude Code）读子账户，
  在 `snapshot` 模式回放这份快照——同样的模型、同样的报告、真实的余额。直连 OAuth 路径
  （`McpBinanceClient`，CIMD，`token_endpoint_auth_method=none`）已写好并测试，等注册开放即可用。
- *真正 live 的部分：* `snapshot.json` 里的每一笔余额都由 **8 笔真实 Convert 订单**经 MCP
  `convert_*` 工具铺出并再平衡（order ID 在文件 `provenance` 里，全部 `orderStatus: SUCCESS`），
  包括分析师在 demo 里提议的那笔削减。

**2. x402 结算跑在 mock 模式。**
402 → 付款 → 200 的握手、支付头、花费账本都是真的；只有链上 USDC 转账是模拟的
（`X402_MODE=mock`——格式正确的假 tx hash，账本照样递减、每日上限照样生效）。这是公开演示
的主动风控选择。切到真链只需一个钱包私钥 + 一个 facilitator——见下方《切换 x402 到真链》，
三个 `# HOOK` 点已在 `x402.py` 标好。

### 目录结构

```
alphabazaar/
  config.py         环境变量驱动的配置
  models.py         共享 pydantic 模型
  marketdata.py     公开 Binance REST + 离线合成回退
  binance_client.py Mock + MCP 子账户客户端
  x402.py           x402 握手（客户端 + 服务端）+ 花费账本
  brain.py          规划器 + 报告综合（确定性 / claude-opus-5）
  analyst.py        买方 agent 编排
  report.py         Report -> HTML
  cli.py            `python -m alphabazaar.cli run`
sellers/
  common.py         FastAPI 的 x402 paywall
  funding_scanner.py  卖方 A
  risk_analyzer.py    卖方 B
run_demo.sh         一条命令的 demo
```

### 切换 x402 到真链

`x402.py` 里三处真实结算点标了 `# HOOK`：

1. `_build_payment_header` —— 用 `X402_WALLET_PRIVATE_KEY` 签一个 EIP-3009 `transferWithAuthorization`。
2. `verify_payment_header` → `_facilitator_verify` —— `POST {facilitator}/verify`。
3. `settle_payment` → `_facilitator_settle` —— `POST {facilitator}/settle`。

设 `X402_MODE=live`、`X402_NETWORK=base`，并给 Agentic 钱包充值。

---

## English

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

### Quickstart

#### 1. Prepare a Binance account (for `snapshot` / `live` mode)

1. Complete KYC on Binance.
2. Open an **Agentic sub-account** from the Agent OS page.
3. Transfer in ~**30–50 USDC** plus a little BTC/ETH/BNB for the demo.

> You can skip this and run everything in **`mock` mode** — fully offline
> (synthetic portfolio, live-or-synthetic public market data). The demo video
> uses **`snapshot` mode**: real sub-account data, read through a supported MCP
> client (see step 3 and *Platform limits we hit*).

#### 2. Install

```bash
git clone <this-repo> alphabazaar && cd alphabazaar
python -m venv .venv && source .venv/bin/activate     # or: uv venv
pip install -r requirements.txt
cp .env.example .env
```

#### 3. Connect the real Agentic sub-account

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

#### 4. Configure `.env`

| Var | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | optional; enables the `claude-opus-5` planner + briefing. Without it a deterministic planner is used. |
| `BINANCE_MODE` | `mock` | `mock` \| `snapshot` \| `live` |
| `BINANCE_SNAPSHOT` | `alphabazaar/snapshot.json` | sub-account capture used by `snapshot` mode |
| `BINANCE_OAUTH_CLIENT_METADATA_URL` | — | **required for `live` OAuth** — public HTTPS CIMD document URL |
| `X402_MODE` | `mock` | `mock` = simulated settlement (fake tx hash), ledger still moves. `live` = real Base settlement via a facilitator. |
| `X402_DAILY_CAP_USDC` | `20` | mirrors Binance x402's $20/day cap; the agent stops buying at this. |
| `SELLER_FUNDING_URL` / `SELLER_RISK_URL` | localhost | point at deployed seller URLs when hosted |

#### 5. Run the whole loop

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

#### 6. Tests

```bash
pytest -q      # offline; no credentials needed
```

### How it uses Binance Agent OS

| Agent OS capability | Where |
|---|---|
| **MCP** — Agentic sub-account balances, 24h tickers, perp funding, Convert | `alphabazaar/mcp_client.py` — `SnapshotBinanceClient` (replay a real capture) + `McpBinanceClient` (direct CIMD OAuth). See *Platform limits we hit*. |
| **x402** — agent-to-agent payment (`exact` scheme, USDC on Base) | `alphabazaar/x402.py`, `sellers/common.py` |
| **Agentic sub-account isolation** — no withdrawal scope, transfers stay in-account | trades are Convert-only + human-approved |
| **Public market data** — tickers / funding / klines (no auth) | `alphabazaar/marketdata.py`, used by the seller agents |

### Platform limits we hit

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

### Layout

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

### Going live with x402

`x402.py` has `# HOOK` markers for the three real-settlement points:

1. `_build_payment_header` — sign an EIP-3009 `transferWithAuthorization` with `X402_WALLET_PRIVATE_KEY`.
2. `verify_payment_header` → `_facilitator_verify` — `POST {facilitator}/verify`.
3. `settle_payment` → `_facilitator_settle` — `POST {facilitator}/settle`.

Set `X402_MODE=live`, `X402_NETWORK=base`, and fund the Agentic wallet.

---

*Not financial advice. Demo software. / 非投资建议，演示软件。*
