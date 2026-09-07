"""Seller-agent discovery.

The buyer doesn't hard-code who to pay — it reads a registry of x402 seller
agents (``alphabazaar/sellers.registry.json`` by default) and picks from it.
Anyone who runs an x402 agent and adds an entry joins the market.

Overrides:
  * ``SELLER_REGISTRY``      — path or https URL to a different registry document
  * ``SELLER_<ID>_URL``      — replace one entry's ``url`` (e.g. ``SELLER_FUNDING_URL``)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

REGISTRY_FILE = Path(__file__).resolve().parent / "sellers.registry.json"


def _raw() -> dict:
    src = os.environ.get("SELLER_REGISTRY", "").strip()
    if src.startswith(("http://", "https://")):
        return httpx.get(src, timeout=10.0).raise_for_status().json()
    return json.loads(Path(src or REGISTRY_FILE).read_text())


def load_sellers() -> list[dict]:
    """Return the seller entries, applying per-entry URL env overrides."""
    out: list[dict] = []
    for e in _raw().get("sellers", []):
        e = dict(e)
        override = os.environ.get(f"SELLER_{e['id'].upper()}_URL", "").strip()
        if override:
            e["url"] = override
        e["endpoint"] = e["url"].rstrip("/") + e.get("path", "/analysis")
        out.append(e)
    return out


def catalog(sellers: list[dict]) -> dict[str, str]:
    """id -> one-line description, for the planner / LLM prompt."""
    return {
        e["id"]: f"{e['name']} — {e['blurb']} (${e['price_usdc']:.2f}); tags: {', '.join(e['tags'])}"
        for e in sellers
    }
