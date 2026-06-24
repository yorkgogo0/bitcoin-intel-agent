"""I/O for the Bitcoin Intelligence Agent. All sources are free; only FRED needs a key."""

import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

MEMPOOL_BASE = "https://mempool.space/api"
FNG_URL = "https://api.alternative.me/fng/"
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
NEWS_FEEDS = ["https://www.coindesk.com/arc/outboundfeeds/rss/", "https://cointelegraph.com/rss"]
COIN_KEYWORDS = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth "],
    "SOL": ["solana", "sol "],
    "HYPE": ["hyperliquid", "hype "],
}
REQUEST_TIMEOUT = 10

INTERVAL_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}


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


def fetch_wallet_state(address):
    """Read-only, public, free for any address - no key, no wallet connection, no trading authority."""
    resp = requests.post(HYPERLIQUID_INFO_URL, json={"type": "clearinghouseState", "user": address}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    positions = []
    for entry in data.get("assetPositions", []):
        pos = entry["position"]
        size = float(pos["szi"])
        if size == 0:
            continue
        positions.append(
            {
                "coin": pos["coin"],
                "side": "Long" if size > 0 else "Short",
                "size": abs(size),
                "leverage": pos["leverage"]["value"],
                "entry_price": float(pos["entryPx"]),
                "position_value_usd": float(pos["positionValue"]),
                "unrealized_pnl": float(pos["unrealizedPnl"]),
                "liquidation_price": float(pos["liquidationPx"]) if pos.get("liquidationPx") else None,
            }
        )
    return {"account_value_usd": float(data["marginSummary"]["accountValue"]), "positions": positions}


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


def fetch_news_headlines(coin, limit=6):
    """Plain RSS, no key needed. One feed being down shouldn't break the others."""
    items = []
    for url in NEWS_FEEDS:
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            for item in root.findall("./channel/item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub_date = item.findtext("pubDate") or ""
                try:
                    published = parsedate_to_datetime(pub_date).astimezone(timezone.utc) if pub_date else None
                except (TypeError, ValueError):
                    published = None
                if title and link:
                    items.append({"title": title, "link": link, "published": published})
        except (requests.exceptions.RequestException, ET.ParseError):
            continue

    # Two feeds merged in fetch order, not recency - sort so the newest shows first
    # regardless of source. Undated items (parse failures) sort last, not first.
    items.sort(key=lambda i: i["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    keywords = COIN_KEYWORDS.get(coin, [])
    relevant = [i for i in items if any(k in i["title"].lower() for k in keywords)]
    general = [i for i in items if i not in relevant]

    result = [dict(i, relevant=True) for i in relevant[:limit]]
    if len(result) < limit:
        result += [dict(i, relevant=False) for i in general[: limit - len(result)]]
    return result
