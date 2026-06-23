"""I/O for the Bitcoin Intelligence Agent. All sources are free; only FRED needs a key."""

import os
import time

import requests

MEMPOOL_BASE = "https://mempool.space/api"
FNG_URL = "https://api.alternative.me/fng/"
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
REQUEST_TIMEOUT = 10

INTERVAL_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def fetch_hl_candles(coin, interval, limit=210):
    """OHLCV straight from Hyperliquid - works for any coin listed there, including ones not on Binance."""
    end = int(time.time() * 1000)
    start = end - INTERVAL_MS[interval] * limit
    resp = requests.post(
        HYPERLIQUID_INFO_URL,
        json={"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": start, "endTime": end}},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return [
        {"time": c["t"], "open": float(c["o"]), "high": float(c["h"]), "low": float(c["l"]), "close": float(c["c"])}
        for c in resp.json()
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


def fetch_hyperliquid_market_ctx(coin="BTC"):
    resp = requests.post(HYPERLIQUID_INFO_URL, json={"type": "metaAndAssetCtxs"}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    universe, contexts = resp.json()
    idx = next(i for i, asset in enumerate(universe["universe"]) if asset["name"] == coin)
    ctx = contexts[idx]
    hourly_rate = float(ctx["funding"])
    mark_price = float(ctx["markPx"])
    return {
        "hourly_rate": hourly_rate,
        "annualized_pct": hourly_rate * 24 * 365 * 100,
        "open_interest_usd": float(ctx["openInterest"]) * mark_price,
        "day_volume_usd": float(ctx["dayNtlVlm"]),
        "mark_price": mark_price,
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
