"""Test isolation: no real network, no real credentials, no shared ledger file.

Every test runs as if fully offline. Market-data calls fall back to the
deterministic synthetic path (that's a supported mode, not a failure), so the
suite is fast and stable regardless of whether api.binance.com is reachable —
which is what made ``pytest`` hang before.
"""

from __future__ import annotations

import socket

import pytest

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}
_real_connect = socket.socket.connect
_real_getaddrinfo = socket.getaddrinfo


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if host not in _ALLOWED_HOSTS:
        raise OSError(f"blocked network connect to {host!r} during tests (offline isolation)")
    return _real_connect(self, address)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)

    def _guarded_getaddrinfo(host, *a, **kw):
        if host not in _ALLOWED_HOSTS:
            raise socket.gaierror(f"blocked DNS lookup of {host!r} during tests")
        return _real_getaddrinfo(host, *a, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    yield


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch):
    """Neutralise anything a developer's local .env might inject."""
    from alphabazaar.config import settings

    monkeypatch.setattr(settings, "binance_mode", "mock", raising=False)
    monkeypatch.setattr(settings, "x402_mode", "mock", raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(settings, "x402_wallet_private_key", "", raising=False)
    monkeypatch.setattr(settings, "ledger_db", ":memory:", raising=False)
    monkeypatch.setattr(settings, "seller_registry", "", raising=False)
    monkeypatch.delenv("SELLER_REGISTRY", raising=False)
    yield


@pytest.fixture(autouse=True)
def _reset_marketdata_cache():
    from alphabazaar import marketdata

    marketdata._CACHE.clear()
    marketdata.reset_provenance()
    yield
