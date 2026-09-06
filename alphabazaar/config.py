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

    # sellers
    seller_funding_url: str = field(
        default_factory=lambda: _get("SELLER_FUNDING_URL", "http://127.0.0.1:8801")
    )
    seller_risk_url: str = field(
        default_factory=lambda: _get("SELLER_RISK_URL", "http://127.0.0.1:8802")
    )

    @property
    def has_brain(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def seller_urls(self) -> dict[str, str]:
        return {"funding": self.seller_funding_url, "risk": self.seller_risk_url}


settings = Settings()
