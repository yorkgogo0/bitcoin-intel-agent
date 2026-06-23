"""I/O for the Bitcoin Intelligence Agent. All sources are free; only FRED needs a key."""

import os

import requests

BINANCE_BASE = "https://data-api.binance.vision/api/v3"
MEMPOOL_BASE = "https://mempool.space/api"
FNG_URL = "https://api.alternative.me/fng/"
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
REQUEST_TIMEOUT = 10


def fetch_klines(symbol, interval, limit=210):
    resp = requests.get(
        f"{BINANCE_BASE}/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return [
        {"open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4])}
        for row in resp.json()
    ]


def fetch_fear_greed():
    resp = requests.get(FNG_URL, params={"limit": 1}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    entry = resp.json()["data"][0]
    return {"value": int(entry["value"]), "label": entry["value_classification"]}


def fetch_onchain_signal():
    resp = requests.get(f"{MEMPOOL_BASE}/v1/difficulty-adjustment", timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return {"difficulty_change_pct": resp.json()["difficultyChange"]}


def fetch_hyperliquid_funding(coin="BTC"):
    resp = requests.post(HYPERLIQUID_INFO_URL, json={"type": "metaAndAssetCtxs"}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    universe, contexts = resp.json()
    idx = next(i for i, asset in enumerate(universe["universe"]) if asset["name"] == coin)
    ctx = contexts[idx]
    hourly_rate = float(ctx["funding"])
    return {
        "hourly_rate": hourly_rate,
        "annualized_pct": hourly_rate * 24 * 365 * 100,
        "open_interest_usd": float(ctx["openInterest"]) * float(ctx["markPx"]),
        "mark_price": float(ctx["markPx"]),
    }


def fetch_fred_latest(series_id):
    """Returns None if FRED_API_KEY isn't set - macro is an optional signal."""
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        return None
    resp = requests.get(
        FRED_OBSERVATIONS_URL,
        params={
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 2,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    obs = [o for o in resp.json()["observations"] if o["value"] != "."]
    if len(obs) < 2:
        return None
    latest, previous = float(obs[0]["value"]), float(obs[1]["value"])
    return {"value": latest, "change_pct": (latest - previous) / previous * 100}
