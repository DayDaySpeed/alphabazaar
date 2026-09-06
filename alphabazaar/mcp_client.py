"""Binance Agent OS connection over MCP (Streamable HTTP + OAuth).

Only imported for ``BINANCE_MODE=snapshot`` / ``live`` — mock mode never needs
the ``mcp`` package.

Two clients live here:

* ``SnapshotBinanceClient`` (``BINANCE_MODE=snapshot``) — replays a real
  sub-account capture from ``snapshot.json``. **This is the working live path.**
  Binance Agent OS gates the MCP endpoint to a fixed client allowlist (Claude,
  Claude Code, Codex, ChatGPT, Cursor, VS Code); a custom OAuth client is rejected
  at consent with *"The AI Agent you are using is not currently supported."* So
  AlphaBazaar reads the sub-account through a supported client and replays it here
  — identical models and report downstream.

* ``McpBinanceClient`` (``BINANCE_MODE=live``) — direct CIMD OAuth, ready for when
  Binance opens client registration:
    1. ``python -m alphabazaar.cli mcp-auth``   — one-time OAuth → ``~/.alphabazaar/mcp-oauth.json``
    2. ``python -m alphabazaar.cli mcp-probe``  — prints the server's real tool names
    3. ``BINANCE_MODE=live python -m alphabazaar.cli run``
  Binance advertises CIMD (``client_id_metadata_document_supported``) and exposes
  no dynamic-registration endpoint, so ``BINANCE_OAUTH_CLIENT_METADATA_URL`` must
  point at a public HTTPS ``oauth-client-metadata.json`` (``client_id`` == that
  URL; ``token_endpoint_auth_method`` == ``none``).

Tested against ``mcp`` 2.1.x. Tool names are matched by keyword (``_find_tool``)
because the documented surface may differ from the wire names; ``mcp-probe`` shows
what the server actually advertises.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import urllib.parse
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp import ClientSession
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from .config import settings
from .models import Holding, MarketQuote, Portfolio, RebalanceAction

TOKEN_FILE = Path.home() / ".alphabazaar" / "mcp-oauth.json"
SNAPSHOT_FILE = Path(__file__).resolve().parent / "snapshot.json"
REDIRECT_PORT = 43110
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"

ASSETS = ["BTC", "ETH", "BNB", "SOL"]

# keyword sets used to locate the server's tools (all substrings must match, lower-cased)
_TOOL = {
    "account": ("account",),
    "ticker": ("ticker",),
    "funding": ("funding",),
    "convert": ("convert",),
}


# --------------------------------------------------------------------------- oauth
class FileTokenStorage(TokenStorage):
    def __init__(self, path: Path = TOKEN_FILE):
        self.path = path

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, default=str))
        try:
            self.path.chmod(0o600)
        except Exception:
            pass

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._load().get("tokens")
        return OAuthToken(**raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        d = self._load()
        d["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._save(d)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._load().get("client")
        return OAuthClientInformationFull(**raw) if raw else None

    async def set_client_info(self, info: OAuthClientInformationFull) -> None:
        d = self._load()
        d["client"] = info.model_dump(mode="json", exclude_none=True)
        self._save(d)


async def _redirect_handler(url: str) -> None:
    print(f"\n  Authorize AlphaBazaar with Binance:\n  {url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _wait_for_callback() -> AuthorizationCodeResult:
    holder: dict[str, str | None] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            q = urllib.parse.parse_qs(parsed.query)
            holder["code"] = q.get("code", [""])[0]
            holder["state"] = q.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h3>AlphaBazaar authorized \xe2\x9c\x94  You can close this tab.</h3>")

        def log_message(self, *a):  # silence
            pass

    server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
    try:
        server.handle_request()
    finally:
        server.server_close()
    return AuthorizationCodeResult(code=holder.get("code", "") or "", state=holder.get("state"))


async def _callback_handler() -> AuthorizationCodeResult:
    return await asyncio.to_thread(_wait_for_callback)


def _client_metadata() -> OAuthClientMetadata:
    # Binance AS metadata: token_endpoint_auth_methods_supported=["none"],
    # grant_types_supported=["authorization_code"] only.
    return OAuthClientMetadata(
        client_name="AlphaBazaar",
        redirect_uris=[REDIRECT_URI],
        grant_types=["authorization_code"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )


def _require_client_metadata_url(explicit: str | None = None) -> str:
    url = (explicit or settings.binance_oauth_client_metadata_url or "").strip()
    if not url:
        raise ValueError(
            "BINANCE_OAUTH_CLIENT_METADATA_URL is required. "
            "Binance Agent OS uses CIMD (no /register). Host "
            "alphabazaar/oauth-client-metadata.json on a public HTTPS URL "
            "(client_id must equal that URL; token_endpoint_auth_method=none) "
            "and set the env var to it."
        )
    return url.rstrip("/")


@asynccontextmanager
async def _session(url: str, *, client_metadata_url: str | None = None):
    metadata_url = _require_client_metadata_url(client_metadata_url)
    oauth = OAuthClientProvider(
        server_url=url,
        client_metadata=_client_metadata(),
        storage=FileTokenStorage(),
        redirect_handler=_redirect_handler,
        callback_handler=_callback_handler,
        client_metadata_url=metadata_url,
    )
    http_client = create_mcp_http_client(auth=oauth)
    async with streamable_http_client(url, http_client=http_client) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _run(coro_fn: Callable[[ClientSession], Awaitable[Any]]) -> Any:
    async def _main():
        async with _session(settings.binance_mcp_url) as session:
            return await coro_fn(session)

    return asyncio.run(_main())


# ----------------------------------------------------------------------- helpers
def _result_payload(res) -> Any:
    if getattr(res, "structured_content", None):
        return res.structured_content
    out: list[Any] = []
    for block in getattr(res, "content", None) or []:
        text = getattr(block, "text", None)
        if text is None:
            continue
        try:
            out.append(json.loads(text))
        except Exception:
            out.append(text)
    if len(out) == 1:
        return out[0]
    return out


async def _find_tool(session: ClientSession, key: str) -> str:
    needles = _TOOL[key]
    tools = (await session.list_tools()).tools
    for t in tools:
        low = t.name.lower()
        if all(n in low for n in needles):
            return t.name
    raise RuntimeError(
        f"no MCP tool matching {needles!r}. Server advertises: {[t.name for t in tools]}. "
        "Adjust alphabazaar/mcp_client.py::_TOOL."
    )


async def _call(session: ClientSession, key: str, **args) -> Any:
    name = await _find_tool(session, key)
    return _result_payload(await session.call_tool(name, args))


def _num(d: dict, *keys: str, default: float = 0.0) -> float:
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                pass
    return default


# ------------------------------------------------------------------------ client
class McpBinanceClient:
    def __init__(self):
        self._snapshot_cache: tuple[Portfolio, list[MarketQuote]] | None = None

    # -- public API expected by analyst.py --------------------------------------
    def get_portfolio(self) -> Portfolio:
        return self._snapshot()[0]

    def get_market(self, assets: list[str]) -> list[MarketQuote]:
        return self._snapshot()[1]

    def execute_convert(self, action: RebalanceAction) -> dict:
        def _do(session):
            return _call(
                session,
                "convert",
                fromAsset=action.from_asset,
                toAsset=action.to_asset,
                fromAmount=action.from_qty,
            )

        return {"submitted": True, "response": _run(_do)}

    def probe(self) -> dict:
        async def _do(session):
            info = session.server_info
            tools = (await session.list_tools()).tools
            return {
                "server": getattr(info, "name", "?"),
                "version": getattr(info, "version", "?"),
                "instructions": (session.instructions or "")[:400],
                "tools": [{"name": t.name, "description": (t.description or "")[:120]} for t in tools],
            }

        return _run(_do)

    # -- internals ------------------------------------------------------------
    def _snapshot(self) -> tuple[Portfolio, list[MarketQuote]]:
        if self._snapshot_cache is None:
            self._snapshot_cache = _run(self._fetch_all)
        return self._snapshot_cache

    async def _fetch_all(self, session: ClientSession) -> tuple[Portfolio, list[MarketQuote]]:
        market: list[MarketQuote] = []
        prices: dict[str, MarketQuote] = {}
        for a in ASSETS:
            sym = f"{a}USDT"
            t = await _call(session, "ticker", symbol=sym)
            t = t[0] if isinstance(t, list) and t else t
            try:
                f = await _call(session, "funding", symbol=sym)
                f = f[0] if isinstance(f, list) and f else f
                funding = round(_num(f, "lastFundingRate", "fundingRate") * 100, 4)
            except Exception:
                funding = None
            q = MarketQuote(
                symbol=sym,
                price=_num(t, "lastPrice", "price", "c"),
                change_24h_pct=_num(t, "priceChangePercent", "P"),
                volume_24h_usdc=_num(t, "quoteVolume", "q"),
                funding_rate_8h_pct=funding,
            )
            market.append(q)
            prices[a] = q

        acct = await _call(session, "account")
        balances = acct.get("balances", acct) if isinstance(acct, dict) else acct
        holdings: list[Holding] = []
        cash = 0.0
        for bal in balances or []:
            asset = bal.get("asset") or bal.get("coin")
            free = _num(bal, "free", "available", "balance")
            if not asset:
                continue
            if asset in ("USDC", "USDT", "BUSD", "FDUSD"):
                cash += free
                continue
            if asset not in prices or free <= 0:
                continue
            q = prices[asset]
            holdings.append(
                Holding(
                    asset=asset,
                    free=free,
                    locked=_num(bal, "locked"),
                    price_usdc=q.price,
                    value_usdc=round(free * q.price, 2),
                    change_24h_pct=q.change_24h_pct,
                )
            )
        sub = "agentic"
        if isinstance(acct, dict):
            sub = acct.get("subAccountId") or acct.get("accountId") or "agentic"
        portfolio = Portfolio(sub_account=str(sub), holdings=holdings, cash_usdc=round(cash, 2))
        return portfolio, market


class SnapshotBinanceClient:
    """Replay a real Agentic sub-account capture (``BINANCE_MODE=snapshot``).

    Binance Agent OS currently gates the MCP endpoint to a fixed allowlist of
    client identities (Claude, Claude Code, Codex, ChatGPT, Cursor, VS Code); a
    custom agent gets ``unsupported agent`` at the consent screen. Until Binance
    opens client registration, AlphaBazaar reads the sub-account through one of
    those supported clients and drops the capture in ``snapshot.json`` — same
    schema the report expects, so ``run`` is byte-for-byte a live read downstream.

    ``execute_convert`` records the intended Convert; run it for real from the
    supported client (the demo does exactly that via the MCP ``convert_*`` tools).
    """

    def __init__(self, path: str | Path | None = None):
        p = Path(path or settings.binance_snapshot or SNAPSHOT_FILE)
        if not p.exists():
            raise FileNotFoundError(
                f"snapshot not found: {p}. Capture the Agentic sub-account with a "
                "whitelisted MCP client (see README) or set BINANCE_SNAPSHOT."
            )
        self._d = json.loads(p.read_text())
        self._sub = str(self._d.get("sub_account", "agentic"))

    def get_portfolio(self) -> Portfolio:
        pf = self._d["portfolio"]
        holdings = [
            Holding(
                asset=h["asset"],
                free=float(h["free"]),
                locked=float(h.get("locked", 0.0)),
                price_usdc=float(h["price_usdc"]),
                value_usdc=float(h["value_usdc"]),
                change_24h_pct=float(h.get("change_24h_pct", 0.0)),
            )
            for h in pf["holdings"]
        ]
        return Portfolio(
            sub_account=self._sub, holdings=holdings, cash_usdc=float(pf["cash_usdc"])
        )

    def get_market(self, assets: list[str]) -> list[MarketQuote]:
        by_symbol = {q["symbol"]: q for q in self._d["market"]}
        out: list[MarketQuote] = []
        for a in assets:
            q = by_symbol.get(f"{a}USDT")
            if not q:
                continue
            out.append(
                MarketQuote(
                    symbol=q["symbol"],
                    price=float(q["price"]),
                    change_24h_pct=float(q.get("change_24h_pct", 0.0)),
                    volume_24h_usdc=float(q.get("volume_24h_usdc", 0.0)),
                    funding_rate_8h_pct=(
                        None if q.get("funding_rate_8h_pct") is None
                        else float(q["funding_rate_8h_pct"])
                    ),
                )
            )
        return out

    def execute_convert(self, action: RebalanceAction) -> dict:
        return {
            "status": "RECORDED",
            "sub_account": self._sub,
            "from": action.from_asset,
            "to": action.to_asset,
            "from_qty": action.from_qty,
            "to_qty": action.est_to_qty,
            "note": (
                "snapshot mode — run this Convert for real from the whitelisted "
                "MCP client (convert_sendQuoteRequest + convert_acceptQuote)"
            ),
        }


def mcp_auth() -> None:
    """Run the OAuth handshake and cache tokens (CIMD, not dynamic registration)."""

    async def _do(session: ClientSession):
        return session.server_info

    info = _run(_do)
    print(f"✔ authorized with {getattr(info, 'name', 'Binance MCP')}; tokens cached at {TOKEN_FILE}")
