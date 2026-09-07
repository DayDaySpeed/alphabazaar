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

# process-wide TTL cache so a seller answering one request (or several sellers in
# one demo run) doesn't refetch the same endpoint — and, once a fetch has failed,
# doesn't re-wait the timeout: the fallback is cached too.
_CACHE: dict[str, tuple[float, object]] = {}
_TTL = 90.0


def _cached(key: str, fn, fallback):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    try:
        val = fn()
    except Exception:
        val = fallback() if callable(fallback) else fallback
    _CACHE[key] = (time.time(), val)
    return val

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


def _spot_ticker_board() -> dict[str, dict]:
    with httpx.Client(timeout=4.0) as c:
        r = c.get(f"{SPOT_BASE}/api/v3/ticker/24hr")
        r.raise_for_status()
        return {row["symbol"]: row for row in r.json()}


def get_spot_tickers(assets: list[str]) -> dict[str, dict]:
    rows = _cached("spot_board", _spot_ticker_board, dict)
    out: dict[str, dict] = {}
    for a in assets:
        sym = UNIVERSE.get(a, (f"{a}USDT",))[0]
        row = rows.get(sym)
        if not row:
            out[a] = _synth_ticker(a) if a in _SYNTH_ANCHOR else {"price": 0.0, "change_24h_pct": 0.0, "volume_24h_usdc": 0.0}
            continue
        out[a] = {
            "price": float(row["lastPrice"]),
            "change_24h_pct": float(row["priceChangePercent"]),
            "volume_24h_usdc": float(row["quoteVolume"]),
        }
    return out


def _premium_index_board() -> dict[str, dict]:
    with httpx.Client(timeout=4.0) as c:
        r = c.get(f"{FUTURES_BASE}/fapi/v1/premiumIndex")
        r.raise_for_status()
        return {row["symbol"]: row for row in r.json()}


def get_funding_rates(assets: list[str]) -> dict[str, float]:
    """Latest perp funding rate as a percent per 8h window (e.g. 0.01 == 0.01%)."""
    rows = _cached("premium_index", _premium_index_board, dict)
    out = {}
    for a in assets:
        sym = UNIVERSE.get(a, (f"{a}USDT", f"{a}USDT"))[1]
        row = rows.get(sym)
        out[a] = round(float(row["lastFundingRate"]) * 100, 4) if row else _synth_funding(a)
    return out


def _fetch_klines(sym: str, interval: str, limit: int) -> list[float]:
    with httpx.Client(timeout=4.0) as c:
        r = c.get(
            f"{SPOT_BASE}/api/v3/klines",
            params={"symbol": sym, "interval": interval, "limit": limit},
        )
        r.raise_for_status()
        return [float(row[4]) for row in r.json()]


def _synth_klines(asset: str, limit: int) -> list[float]:
    px = _SYNTH_ANCHOR[asset]
    out = []
    for i in range(limit):
        px *= 1 + _wobble(f"{asset}-walk-{i}", 0.012)
        out.append(round(px, 2))
    return out


def get_klines(asset: str, interval: str = "1h", limit: int = 168) -> list[float]:
    """Return a list of close prices; synthetic fallback is a random walk."""
    sym = UNIVERSE[asset][0]
    return _cached(
        f"klines:{sym}:{interval}:{limit}",
        lambda: _fetch_klines(sym, interval, limit),
        lambda: _synth_klines(asset, limit),
    )


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
