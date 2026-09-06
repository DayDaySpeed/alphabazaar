"""Public Binance market data with an offline synthetic fallback.

These endpoints are public and need no authentication. When the network is
unavailable (or Binance is geo-blocked) we fall back to deterministic synthetic
data so the whole demo still runs end to end.
"""

from __future__ import annotations

import hashlib
import math
import time

import httpx

SPOT_BASE = "https://api.binance.com"
FUTURES_BASE = "https://fapi.binance.com"

# asset -> spot symbol / perp symbol
UNIVERSE = {
    "BTC": ("BTCUSDT", "BTCUSDT"),
    "ETH": ("ETHUSDT", "ETHUSDT"),
    "BNB": ("BNBUSDT", "BNBUSDT"),
    "SOL": ("SOLUSDT", "SOLUSDT"),
}

_SYNTH_ANCHOR = {"BTC": 64000.0, "ETH": 3100.0, "BNB": 580.0, "SOL": 145.0}


def _wobble(key: str, spread: float) -> float:
    """Deterministic-ish pseudo-random in [-spread, spread], drifts slowly over time."""
    bucket = int(time.time() // 900)  # changes every 15 min
    h = hashlib.sha256(f"{key}:{bucket}".encode()).digest()
    unit = int.from_bytes(h[:4], "big") / 0xFFFFFFFF  # [0, 1]
    return (unit * 2 - 1) * spread


def _synth_ticker(asset: str) -> dict:
    px = _SYNTH_ANCHOR[asset] * (1 + _wobble(f"{asset}-px", 0.06))
    chg = _wobble(f"{asset}-chg", 7.5)
    vol = (5_000_000_000 if asset == "BTC" else 900_000_000) * (1 + _wobble(f"{asset}-vol", 0.4))
    return {"price": round(px, 2), "change_24h_pct": round(chg, 2), "volume_24h_usdc": round(vol)}


def _synth_funding(asset: str) -> float:
    # basis points-ish; occasionally spikes to make the demo interesting
    base = _wobble(f"{asset}-fund", 0.03)
    spike = 0.09 if _wobble(f"{asset}-spike", 1.0) > 0.7 else 0.0
    return round(base + spike, 4)


def get_spot_tickers(assets: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        with httpx.Client(timeout=6.0) as c:
            r = c.get(f"{SPOT_BASE}/api/v3/ticker/24hr")
            r.raise_for_status()
            rows = {row["symbol"]: row for row in r.json()}
        for a in assets:
            sym = UNIVERSE[a][0]
            row = rows.get(sym)
            if not row:
                out[a] = _synth_ticker(a)
                continue
            out[a] = {
                "price": float(row["lastPrice"]),
                "change_24h_pct": float(row["priceChangePercent"]),
                "volume_24h_usdc": float(row["quoteVolume"]),
            }
        return out
    except Exception:
        return {a: _synth_ticker(a) for a in assets}


def get_funding_rates(assets: list[str]) -> dict[str, float]:
    """Latest perp funding rate as a percent per 8h window (e.g. 0.01 == 0.01%)."""
    try:
        with httpx.Client(timeout=6.0) as c:
            r = c.get(f"{FUTURES_BASE}/fapi/v1/premiumIndex")
            r.raise_for_status()
            rows = {row["symbol"]: row for row in r.json()}
        out = {}
        for a in assets:
            sym = UNIVERSE[a][1]
            row = rows.get(sym)
            out[a] = round(float(row["lastFundingRate"]) * 100, 4) if row else _synth_funding(a)
        return out
    except Exception:
        return {a: _synth_funding(a) for a in assets}


def get_klines(asset: str, interval: str = "1h", limit: int = 168) -> list[float]:
    """Return a list of close prices; synthetic fallback is a random walk."""
    sym = UNIVERSE[asset][0]
    try:
        with httpx.Client(timeout=6.0) as c:
            r = c.get(
                f"{SPOT_BASE}/api/v3/klines",
                params={"symbol": sym, "interval": interval, "limit": limit},
            )
            r.raise_for_status()
            return [float(row[4]) for row in r.json()]
    except Exception:
        anchor = _SYNTH_ANCHOR[asset]
        out = []
        px = anchor
        for i in range(limit):
            px *= 1 + _wobble(f"{asset}-walk-{i}", 0.012)
            out.append(round(px, 2))
        return out


def realized_vol_pct(closes: list[float]) -> float:
    """Annualised realized volatility (%) from hourly closes."""
    if len(closes) < 3:
        return 0.0
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1]]
    if not rets:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / len(rets)
    hourly_sd = math.sqrt(var)
    return round(hourly_sd * math.sqrt(24 * 365) * 100, 1)


def correlation(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a, b = a[-n:], b[-n:]
    ra = [a[i] / a[i - 1] - 1 for i in range(1, n) if a[i - 1]]
    rb = [b[i] / b[i - 1] - 1 for i in range(1, n) if b[i - 1]]
    m = min(len(ra), len(rb))
    ra, rb = ra[-m:], rb[-m:]
    if m < 3:
        return 0.0
    ma, mb = sum(ra) / m, sum(rb) / m
    cov = sum((ra[i] - ma) * (rb[i] - mb) for i in range(m))
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((x - mb) ** 2 for x in rb)
    if va <= 0 or vb <= 0:
        return 0.0
    return round(cov / math.sqrt(va * vb), 2)
