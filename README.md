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
┌───────────────────────────┐  x402: 402 → 付 USDC → 200   ┌────────────────────────────┐
│  分析师 Agent（买方）      │ ───────────────────────────────▶ │ 卖方 · 资金费率扫描器       │ $1.50
│  · Binance MCP 客户端      │ ───────────────────────────────▶ │ 卖方 · 风险分析器           │ $1.75
│  · 读子账户                │ ───────────────────────────────▶ │ 卖方 · 动量/趋势扫描器      │ $1.25
│  · 从注册表发现卖方        │                                 └────────────────────────────┘
│  · 规划买什么 → 用 x402 付 │    卖方在 sellers.registry.json 中登记；加一条即入场
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
> （见第 3 步和《遇到的平台限制》）。

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

**`snapshot` 模式** 用受支持的客户端读子账户，让 AlphaBazaar 回放这份快照：

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
./run_demo.sh          # 启动本地三个卖方 agent，跑分析师，然后收尾
```

分析师读子账户 → 从注册表**发现**卖方 → 用 x402 付每个卖方 → 写 `reports/YYYY-MM-DD.html`
→ 打印再平衡建议并请求批准（输 `y` 在**子账户内**执行一笔 Convert）。

**对着已部署的公网卖方跑**（见《部署卖方》），不需要本地起服务：

```bash
SELLER_FUNDING_URL=https://alphabazaar-funding-scanner.onrender.com \
SELLER_RISK_URL=https://alphabazaar-risk-analyzer.onrender.com \
SELLER_MOMENTUM_URL=https://alphabazaar-momentum-scanner.onrender.com \
BINANCE_MODE=snapshot X402_MODE=mock \
  python -m alphabazaar.cli run
# 注册表已内置这三个公网 URL；bare `python -m alphabazaar.cli run` 直接对着云上跑。
# 换一份托管注册表：SELLER_REGISTRY=https://raw.githubusercontent.com/<你>/alphabazaar/main/alphabazaar/sellers.registry.json
```

#### 6. 测试

```bash
pytest -q      # 离线；无需凭证
```

### 如何用到 Binance Agent OS

| Agent OS 能力 | 用在哪 |
|---|---|
| **MCP** —— Agentic 子账户余额、24h 行情、永续资金费率、Convert | `alphabazaar/mcp_client.py` —— `SnapshotBinanceClient`（回放真实快照）+ `McpBinanceClient`（直连 CIMD OAuth）。见《我们遇到的平台限制》。 |
| **x402** —— agent 对 agent 支付（v2 `exact` scheme，EIP-3009 gasless USDC）。`mock`=HMAC 模拟；`live`=EIP-712 签名 + facilitator 链上结算 | `alphabazaar/x402.py`、`sellers/common.py` |
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

**2. x402 结算：演示走 mock，真链已实现。**
`X402_MODE=live` 是完整的 x402 v2 实现 —— EIP-712 签名 + facilitator 链上结算，
已对 `https://x402.org/facilitator` 验证通过（Base Sepolia 接受我们的 payload 和签名，
只在余额检查处停下）。演示默认 `mock`（HMAC 模拟签名 + 假 tx hash，账本照样递减、
每日上限照样生效）纯粹是公开演示的风控选择。跑真链见《真实 x402 结算》。

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
  registry.py       seller discovery (reads sellers.registry.json)
sellers/
  common.py         FastAPI 的 x402 paywall
  funding_scanner.py / risk_analyzer.py / momentum_scanner.py   三个卖方 agent
sellers.registry.json  卖方登记表   ·   render.yaml  一键部署三个卖方到公网
run_demo.sh         一条命令的 demo
```

### 部署卖方

`render.yaml` 是一份 [Render](https://render.com) Blueprint。Render → **New → Blueprint**
→ 连接本仓库 → 它读到 `render.yaml`，创建三个免费 Web 服务
（`uvicorn sellers.<name>:app`，`PYTHON_VERSION=3.12`）。首次部署会让你填三个
`SELLER_*_PAY_TO`（`mock` 演示随便填个地址即可）。拿到三个
`https://alphabazaar-*.onrender.com` URL 后，见上面第 5 步。

- 免费档 15 分钟无请求会休眠，首次请求冷启动 ~30–60 秒 —— 演示前先各访问一次 `/` 唤醒。
- 云上跑真链 x402：每个服务面板里设 `X402_MODE=live` + 真实 `SELLER_*_PAY_TO`。

### 真实 x402 结算（Base Sepolia）

`X402_MODE=live` 是完整实现，不是占位：`_build_payment_header` 用
`X402_WALLET_PRIVATE_KEY` 对 EIP-3009 `TransferWithAuthorization` 做 EIP-712 签名，
卖方把 x402 v2 payload 交给 facilitator（默认 `https://x402.org/facilitator`），
facilitator 在链上提交无 gas 的 `transferWithAuthorization` 并返回 tx hash。

跑一笔真实结算：

```bash
# 1. 给付款钱包充测试 USDC（无需 ETH，EIP-3009 是无 gas 的）：https://faucet.circle.com
# 2. 启动一个 live 模式的卖方：
X402_MODE=live SELLER_FUNDING_PAY_TO=0x... \
  python -m uvicorn sellers.funding_scanner:app --port 8801 &
# 3. 打一笔：
X402_MODE=live X402_WALLET_PRIVATE_KEY=0x... \
  python -m alphabazaar.cli x402-selftest
# → 打印 https://sepolia.basescan.org/tx/0x... 
```

**已在链上验证**（Base Sepolia，2026-09-06）—— 一次完整 demo 循环的两笔真实 A2A 付款：

| 付款 | 金额 | tx |
|---|---|---|
| 分析师 → 资金费率扫描器 | 1.5 USDC | [`0x731756e1…`](https://sepolia.basescan.org/tx/0x731756e1b97ee275596413abc552a0ac69336f187e6ece9ec933c6e64bff8962) |
| 分析师 → 风险分析器 | 1.75 USDC | [`0x0d35528b…`](https://sepolia.basescan.org/tx/0x0d35528bd069c0e641ac48ea9ac3311bdf9ee863397d63807ee7ee7a9f280731) |

由 facilitator（`0xd407…f1bf`）代付 gas 提交 `transferWithAuthorization`；付款钱包
无需持有 ETH。

`base` 主网：把 `X402_NETWORK=base`、换一个支持主网的 facilitator、钱包放真 USDC。

---

## English

Your portfolio-analyst agent connects to your Binance **Agentic sub-account**, reads
your holdings and live market data, decides which specialist analyses it needs, then
**autonomously pays other AI agents via x402** (~$1–3 each) to buy that analysis — and
hands you an actionable daily report. All trades are human-approved and stay inside an
isolated sub-account with **no withdrawal permissions**.

Submitted to the **Binance Agent OS mini hackathon** · Track: *Payment workflow (agent-to-agent payments)*.

```
┌───────────────────────────┐  x402: 402 → pay USDC → 200  ┌────────────────────────────┐
│  Analyst agent (buyer)    │ ───────────────────────────────▶ │ Seller · Funding scanner   │ $1.50
│  · Binance MCP client     │ ───────────────────────────────▶ │ Seller · Risk analyzer     │ $1.75
│  · reads sub-account      │ ───────────────────────────────▶ │ Seller · Momentum scanner  │ $1.25
│  · discovers sellers      │                                 └────────────────────────────┘
│  · plans → pays via x402  │    sellers listed in sellers.registry.json; add one to join
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
./run_demo.sh          # starts the three local seller agents, runs the analyst, tears down
```

The analyst reads the sub-account → **discovers** sellers from the registry →
pays each over x402 → writes `reports/YYYY-MM-DD.html` → prints any proposed
rebalance and asks for approval (`y` executes a Convert **inside the sub-account**).

**Against the deployed public sellers** (see *Deploying the sellers*) — no local
services needed:

```bash
SELLER_FUNDING_URL=https://alphabazaar-funding-scanner.onrender.com \
SELLER_RISK_URL=https://alphabazaar-risk-analyzer.onrender.com \
SELLER_MOMENTUM_URL=https://alphabazaar-momentum-scanner.onrender.com \
BINANCE_MODE=snapshot X402_MODE=mock \
  python -m alphabazaar.cli run
# the registry already ships these public URLs, so a bare `python -m alphabazaar.cli run`
# hits the cloud market directly. Swap registries: SELLER_REGISTRY=https://…/sellers.registry.json
```

#### 6. Tests

```bash
pytest -q      # offline; no credentials needed
```

### How it uses Binance Agent OS

| Agent OS capability | Where |
|---|---|
| **MCP** — Agentic sub-account balances, 24h tickers, perp funding, Convert | `alphabazaar/mcp_client.py` — `SnapshotBinanceClient` (replay a real capture) + `McpBinanceClient` (direct CIMD OAuth). See *Platform limits we hit*. |
| **x402** — agent-to-agent payment (v2 `exact` scheme, gasless EIP-3009 USDC). `mock` = HMAC sim; `live` = EIP-712 signature + facilitator on-chain settlement | `alphabazaar/x402.py`, `sellers/common.py` |
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

**2. x402 settlement: the demo runs mock, live is implemented.**
`X402_MODE=live` is a complete x402 v2 implementation — EIP-712 signature +
facilitator on-chain settlement — verified against `https://x402.org/facilitator`
(Base Sepolia accepts our payload and signature and stops only at the balance
check). The demo defaults to `mock` (HMAC signature + fake tx hash; the ledger
still decrements and the daily cap still bites) purely as a public-demo risk
choice. Run it for real: *Real x402 settlement* below.

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
  registry.py       seller discovery (reads sellers.registry.json)
sellers/
  common.py         x402 paywall for FastAPI
  funding_scanner.py / risk_analyzer.py / momentum_scanner.py   the three seller agents
sellers.registry.json   the seller directory   ·   render.yaml  deploy all three publicly
run_demo.sh         one-command demo
```

### Deploying the sellers

`render.yaml` is a [Render](https://render.com) Blueprint. Render → **New →
Blueprint** → connect this repo → it reads `render.yaml` and creates three free
web services (`uvicorn sellers.<name>:app`, `PYTHON_VERSION=3.12`). The first
deploy prompts for the three `SELLER_*_PAY_TO` values (any address is fine for a
`mock` demo). Take the three `https://alphabazaar-*.onrender.com` URLs and use
them in step 5 above.

- Free tier sleeps after 15 min idle; the first request cold-starts in ~30–60s —
  hit each `/` once before a demo to wake them.
- For live x402 on the cloud: set `X402_MODE=live` + a real `SELLER_*_PAY_TO` in
  each service's dashboard.

### Real x402 settlement (Base Sepolia)

`X402_MODE=live` is a full implementation, not a stub: `_build_payment_header`
EIP-712-signs an EIP-3009 `TransferWithAuthorization` with
`X402_WALLET_PRIVATE_KEY`; the seller hands the x402 v2 payload to a facilitator
(default `https://x402.org/facilitator`) which submits the gasless
`transferWithAuthorization` on-chain and returns the tx hash.

Run one real settlement:

```bash
# 1. fund the payer wallet with test USDC (no ETH needed — EIP-3009 is gasless):
#    https://faucet.circle.com  (Base Sepolia)
# 2. start a seller in live mode:
X402_MODE=live SELLER_FUNDING_PAY_TO=0x... \
  python -m uvicorn sellers.funding_scanner:app --port 8801 &
# 3. pay it:
X402_MODE=live X402_WALLET_PRIVATE_KEY=0x... \
  python -m alphabazaar.cli x402-selftest
# → prints https://sepolia.basescan.org/tx/0x...
```

**Verified on-chain** (Base Sepolia, 2026-09-06) — the two real A2A payments from
one full demo loop:

| Payment | Amount | tx |
|---|---|---|
| analyst → funding scanner | 1.5 USDC | [`0x731756e1…`](https://sepolia.basescan.org/tx/0x731756e1b97ee275596413abc552a0ac69336f187e6ece9ec933c6e64bff8962) |
| analyst → risk analyzer | 1.75 USDC | [`0x0d35528b…`](https://sepolia.basescan.org/tx/0x0d35528bd069c0e641ac48ea9ac3311bdf9ee863397d63807ee7ee7a9f280731) |

The facilitator (`0xd407…f1bf`) pays gas to submit `transferWithAuthorization`;
the payer wallet holds no ETH.

For `base` mainnet: set `X402_NETWORK=base`, point at a mainnet-capable
facilitator, and fund the wallet with real USDC.

---

*Not financial advice. Demo software. / 非投资建议，演示程序。*
