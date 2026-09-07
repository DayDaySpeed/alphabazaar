"""Seller-agent discovery."""

import json

import pytest

from alphabazaar import registry


def test_bundled_registry_has_three_sellers_with_endpoints(monkeypatch):
    monkeypatch.delenv("SELLER_REGISTRY", raising=False)
    for k in ("SELLER_FUNDING_URL", "SELLER_RISK_URL", "SELLER_MOMENTUM_URL"):
        monkeypatch.delenv(k, raising=False)

    sellers = registry.load_sellers()
    ids = [e["id"] for e in sellers]
    assert ids == ["funding", "risk", "momentum"]
    for e in sellers:
        assert e["endpoint"] == e["url"].rstrip("/") + e["path"]
        assert e["url"].startswith("http")
        assert e["price_usdc"] > 0 and e["tags"] and e["blurb"]

    cat = registry.catalog(sellers)
    assert set(cat) == set(ids)
    assert "tags:" in cat["funding"]


def test_per_entry_url_override(monkeypatch):
    monkeypatch.delenv("SELLER_REGISTRY", raising=False)
    monkeypatch.setenv("SELLER_MOMENTUM_URL", "https://momentum.example.com")
    by_id = {e["id"]: e for e in registry.load_sellers()}
    assert by_id["momentum"]["url"] == "https://momentum.example.com"
    assert by_id["momentum"]["endpoint"] == "https://momentum.example.com/analysis"
    assert by_id["funding"]["url"] != "https://momentum.example.com"  # untouched


def test_registry_file_override(monkeypatch, tmp_path):
    doc = {"sellers": [
        {"id": "x", "name": "X", "url": "http://x", "path": "/a", "price_usdc": 0.1,
         "tags": ["t"], "blurb": "b"}
    ]}
    p = tmp_path / "reg.json"
    p.write_text(json.dumps(doc))
    monkeypatch.setenv("SELLER_REGISTRY", str(p))
    sellers = registry.load_sellers()
    assert [e["id"] for e in sellers] == ["x"]
    assert sellers[0]["endpoint"] == "http://x/a"
