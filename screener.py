"""Lightweight multi-asset screener - cheap signals only (technical + funding + ICT
structure), not the full run_analysis() stack (skips news/macro/ATH-ATL/on-chain/
relative-strength/OI-trend - those need per-coin config or accumulated history that
doesn't scale cheaply across many assets). Use run_analysis() for a full deep-dive on
any candidate this surfaces.

Universe is capped to the top-volume assets by design: of Hyperliquid's ~230 tradable
perps, the top 25 by 24h volume capture ~97-98% of total trading activity, and the long
tail includes literally zero-volume markets. Scanning the full 230 would mostly compute
precise-looking scores for dead markets, not find more real opportunity.
"""

import requests

from bitcoin_intel_agent import compute_ict_structure
from data_sources import HYPERLIQUID_INFO_URL, fetch_hl_candles
from ict import nearest_target
from indicators import atr, bollinger_bands, macd_histogram, rsi, sma, stoch_rsi

REQUEST_TIMEOUT = 10
TIMEFRAME_WEIGHTS = {"1h": 0.25, "4h": 0.35, "1d": 0.40}


def fetch_universe(top_n=25):
    resp = requests.post(HYPERLIQUID_INFO_URL, json={"type": "metaAndAssetCtxs"}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    universe, contexts = resp.json()
    ranked = sorted(zip(universe["universe"], contexts), key=lambda p: float(p[1]["dayNtlVlm"]), reverse=True)
    result = []
    for asset, ctx in ranked[:top_n]:
        hourly_rate = float(ctx["funding"])
        result.append({
            "coin": asset["name"],
            "day_volume_usd": float(ctx["dayNtlVlm"]),
            "funding_annualized_pct": hourly_rate * 24 * 365 * 100,
            "open_interest_usd": float(ctx["openInterest"]) * float(ctx["markPx"]),
        })
    return result


def _quick_timeframe_score(candles):
    closes = [c["close"] for c in candles]
    price = closes[-1]
    sma20, sma50 = sma(closes, 20), sma(closes, 50)
    rsi14 = rsi(closes)
    stoch = stoch_rsi(closes)
    hist = macd_histogram(closes)
    bands = bollinger_bands(closes)

    score = 50.0
    if sma20 and sma50:
        if price > sma20 > sma50:
            score += 15
        elif price < sma20 < sma50:
            score -= 15
    if rsi14 is not None:
        if rsi14 >= 70:
            score -= 5
        elif rsi14 <= 30:
            score += 5
    if stoch is not None:
        if stoch >= 0.8:
            score -= 5
        elif stoch <= 0.2:
            score += 5
    if hist is not None:
        score += 10 if hist > 0 else -10
    if bands is not None:
        if price > bands["upper"]:
            score -= 5
        elif price < bands["lower"]:
            score += 5

    return max(0.0, min(100.0, score)), price


def quick_score(market_ctx):
    """One asset's lightweight score. Raises requests.RequestException on data failure -
    caller should skip that asset, not crash the whole scan."""
    coin = market_ctx["coin"]
    timeframe_scores = {}
    daily_candles, daily_price, daily_atr = None, None, None

    for interval, weight in TIMEFRAME_WEIGHTS.items():
        candles = fetch_hl_candles(coin, interval, limit=210)
        score, price = _quick_timeframe_score(candles)
        timeframe_scores[interval] = score
        if interval == "1d":
            daily_candles, daily_price, daily_atr = candles, price, atr(candles)

    weighted = sum(s * TIMEFRAME_WEIGHTS[i] for i, s in timeframe_scores.items())

    annualized = market_ctx["funding_annualized_pct"]
    weighted += max(-8.0, min(8.0, -annualized * 0.1))

    ict = compute_ict_structure(daily_candles, daily_price)
    structure_tilt = {"bullish_bos": 8, "bullish_choch": 6, "bearish_bos": -8, "bearish_choch": -6}
    weighted += structure_tilt.get(ict["structure"]["signal"], 0)
    if ict["zone"] is not None:
        weighted += 3 if ict["zone"]["zone"] == "discount" else -3

    scores = list(timeframe_scores.values())
    disagreement = max(scores) - min(scores)
    confidence = max(10.0, 100 - disagreement - min(20.0, abs(annualized) * 0.2))
    vol_pct = (daily_atr / daily_price * 100) if daily_atr else 0
    risk_score = max(0.0, min(100.0, 30 + disagreement * 0.3 + min(20.0, vol_pct * 3) + min(15.0, abs(annualized) * 0.15)))
    bull_score = max(0.0, min(100.0, weighted))

    bias = "Long" if bull_score >= 60 else "Short" if bull_score <= 40 else None
    target, invalidation, risk_reward = None, None, None
    if bias and daily_atr:
        invalidation = daily_price - 1.5 * daily_atr if bias == "Long" else daily_price + 1.5 * daily_atr
        stop_distance = abs(daily_price - invalidation)
        target = nearest_target(bias, daily_price, ict["pools_above"] + ict["pools_below"], ict["open_gaps"], min_distance=stop_distance)
        if target:
            risk_reward = abs(target - daily_price) / stop_distance if stop_distance else None

    return {
        "coin": coin,
        "price": daily_price,
        "bull_score": bull_score,
        "bear_score": 100 - bull_score,
        "risk_score": risk_score,
        "confidence": confidence,
        "signal_quality": sum(1 for s in scores if abs(s - 50) > 10),
        "structure_signal": ict["structure"]["signal"],
        "funding_annualized_pct": annualized,
        "day_volume_usd": market_ctx["day_volume_usd"],
        "open_interest_usd": market_ctx["open_interest_usd"],
        "risk_reward": risk_reward,
        "bias": bias,
    }


def scan(top_n=25):
    """Scans the top-volume universe. Returns (results, skipped) - skipped assets had a
    data error and were dropped rather than silently shown with fabricated numbers."""
    universe = fetch_universe(top_n)
    results, skipped = [], []
    for market_ctx in universe:
        try:
            results.append(quick_score(market_ctx))
        except requests.exceptions.RequestException as exc:
            skipped.append({"coin": market_ctx["coin"], "error": str(exc)})
    return results, skipped
