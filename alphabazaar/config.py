"""Environment-driven settings for AlphaBazaar."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional, but convenient for local dev
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = REPO_ROOT / "reports"
RUN_DIR = REPORT_DIR / "runs"
STATE_DIR = REPO_ROOT / "var"


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    # buyer brain
    anthropic_api_key: str = field(default_factory=lambda: _get("ANTHROPIC_API_KEY"))
    model: str = field(default_factory=lambda: _get("ALPHABAZAAR_MODEL", "claude-opus-5"))

    # binance agent os — mode is one of: mock | snapshot | live
    #   mock     synthetic portfolio, fully offline
    #   snapshot replay a real Agentic sub-account capture (BINANCE_SNAPSHOT / snapshot.json)
    #            taken through a whitelisted MCP client — this is what the demo uses
    #   live     connect the MCP endpoint directly (CIMD OAuth); blocked until Binance
    #            opens Agent OS to third-party client identities
    binance_mode: str = field(default_factory=lambda: _get("BINANCE_MODE", "mock").lower())
    binance_snapshot: str = field(default_factory=lambda: _get("BINANCE_SNAPSHOT"))
    binance_oauth_client_metadata_url: str = field(
        default_factory=lambda: _get("BINANCE_OAUTH_CLIENT_METADATA_URL")
    )
    binance_mcp_url: str = field(
        default_factory=lambda: _get("BINANCE_MCP_URL", "https://agent.binance.com/mcp/agentic")
    )

    # x402
    x402_mode: str = field(default_factory=lambda: _get("X402_MODE", "mock").lower())
    x402_network: str = field(default_factory=lambda: _get("X402_NETWORK", "base-sepolia"))
    x402_wallet_private_key: str = field(default_factory=lambda: _get("X402_WALLET_PRIVATE_KEY"))
    x402_facilitator_url: str = field(
        default_factory=lambda: _get("X402_FACILITATOR_URL", "https://x402.org/facilitator")
    )
    x402_daily_cap_usdc: float = field(
        default_factory=lambda: float(_get("X402_DAILY_CAP_USDC", "20") or 20)
    )
    x402_mock_secret: str = field(
        default_factory=lambda: _get("X402_MOCK_SECRET", "alphabazaar-dev-secret")
    )
    # hard ceiling on any single seller call (quote-cap check, before paying)
    x402_max_price_usdc: float = field(
        default_factory=lambda: float(_get("X402_MAX_PRICE_USDC", "5") or 5)
    )
    # SSRF guard: by default the buyer only pays sellers on loopback or a public
    # address. Set to 1 to also allow RFC-1918 private ranges (never link-local /
    # cloud-metadata, which stay blocked).
    x402_allow_private_sellers: bool = field(
        default_factory=lambda: _get("X402_ALLOW_PRIVATE_SELLERS", "").lower() in ("1", "true", "yes")
    )
    # persistent budget + payment ledger (SQLite). ":memory:" disables persistence.
    ledger_db: str = field(
        default_factory=lambda: _get("ALPHABAZAAR_LEDGER_DB", str(STATE_DIR / "ledger.db"))
    )

    # trade validation / execution guard
    trade_min_notional_usdc: float = field(
        default_factory=lambda: float(_get("TRADE_MIN_NOTIONAL_USDC", "1") or 1)
    )
    trade_max_notional_usdc: float = field(
        default_factory=lambda: float(_get("TRADE_MAX_NOTIONAL_USDC", "0") or 0)  # 0 == unset
    )
    # market/snapshot data older than this many hours blocks live trade execution
    data_max_age_hours: float = field(
        default_factory=lambda: float(_get("DATA_MAX_AGE_HOURS", "24") or 24)
    )

    # sellers — discovery is via alphabazaar/sellers.registry.json (or SELLER_REGISTRY);
    # these are per-entry URL overrides, also honoured by registry.load_sellers().
    seller_registry: str = field(default_factory=lambda: _get("SELLER_REGISTRY"))
    seller_funding_url: str = field(
        default_factory=lambda: _get("SELLER_FUNDING_URL", "http://127.0.0.1:8801")
    )
    seller_risk_url: str = field(
        default_factory=lambda: _get("SELLER_RISK_URL", "http://127.0.0.1:8802")
    )
    seller_momentum_url: str = field(
        default_factory=lambda: _get("SELLER_MOMENTUM_URL", "http://127.0.0.1:8803")
    )

    @property
    def has_brain(self) -> bool:
        return bool(self.anthropic_api_key)

    def validate(self) -> list[str]:
        """Return a list of configuration problems (empty == OK)."""
        problems: list[str] = []
        if self.binance_mode not in ("mock", "snapshot", "live"):
            problems.append(f"BINANCE_MODE must be mock|snapshot|live, got {self.binance_mode!r}")
        if self.x402_mode not in ("mock", "live"):
            problems.append(f"X402_MODE must be mock|live, got {self.x402_mode!r}")
        if self.x402_network not in ("base", "base-sepolia"):
            problems.append(f"X402_NETWORK must be base|base-sepolia, got {self.x402_network!r}")
        if self.x402_daily_cap_usdc <= 0:
            problems.append("X402_DAILY_CAP_USDC must be > 0")
        if self.x402_max_price_usdc <= 0:
            problems.append("X402_MAX_PRICE_USDC must be > 0")
        if self.x402_mode == "live" and not self.x402_wallet_private_key:
            problems.append("X402_MODE=live requires X402_WALLET_PRIVATE_KEY")
        if self.binance_mode == "live" and not self.binance_oauth_client_metadata_url:
            problems.append("BINANCE_MODE=live requires BINANCE_OAUTH_CLIENT_METADATA_URL")
        if self.data_max_age_hours <= 0:
            problems.append("DATA_MAX_AGE_HOURS must be > 0")
        return problems


settings = Settings()
