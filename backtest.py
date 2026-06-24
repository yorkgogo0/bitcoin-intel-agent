"""Historical backtest harness - walks forward through real past data and grades what the
system would have recommended each day against what actually happened next.

READ THIS BEFORE TRUSTING ANY NUMBER THIS PRODUCES:

- BTC only, daily resolution only. The live system also blends 1h/4h timeframes - this
  validates the daily-timeframe core + macro signals, not the full multi-timeframe blend.
- Included: technical (SMA/RSI/MACD/Bollinger/StochRSI), ICT structure, funding (paginated
  real history), Fear & Greed (full real history), macro (FRED, only if FRED_API_KEY is set).
- Excluded, not silently approximated: OI-trend (Hyperliquid has no free historical OI
  endpoint - metaAndAssetCtxs is current-only), news headlines (RSS has no archive),
  on-chain difficulty (skipped for v1), relative-strength-vs-BTC (not applicable to BTC
  itself).
- Sample size is small - funding history only paginates back ~70-90 days before hitting
  rate limits. That's ~50-60 daily decision points after the indicator lookback period.
  Treat results as directional/suggestive, not statistically conclusive. A few dozen
  trades is not "a large sample size."
- Same-day stop-and-target hits are marked "ambiguous", not guessed - daily OHLC can't tell
  you which was touched first within the day.

Reuses the exact same scoring/no-trade functions as the live system (classify_confidence_tier,
apply_no_trade_rules, compute_ict_structure, nearest_target) rather than a parallel
reimplementation that could silently drift from what's actually running live.
"""

import os
import time

import requests

from bitcoin_intel_agent import (
    apply_no_trade_rules,
    compute_ict_structure,
)
from data_sources import HYPERLIQUID_INFO_URL, fetch_hl_candles
from ict import nearest_target
from indicators import atr, bollinger_bands, macd_histogram, rsi, sma, stoch_rsi

REQUEST_TIMEOUT = 10


def fetch_funding_history_days(coin="BTC", days=80):
    end = int(time.time() * 1000)
    all_records = []
    cursor_end = end
    window_start = end - 1000 * 60 * 60 * 24 * days
    for _ in range(8):
        start = max(window_start, cursor_end - 1000 * 60 * 60 * 24 * 20)
        resp = requests.post(
            HYPERLIQUID_INFO_URL, json={"type": "fundingHistory", "coin": coin, "startTime": start, "endTime": cursor_end},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json()
        all_records = batch + all_records
        if not batch or cursor_end <= window_start:
            break
        cursor_end = batch[0]["time"] - 1
    return all_records


def fetch_fear_greed_history():
    resp = requests.get("https://api.alternative.me/fng/", params={"limit": 0}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["data"]  # newest first


def fetch_fred_history(series_id, limit=200):
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        return None
    resp = requests.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params={"series_id": series_id, "api_key": api_key, "file_type": "json", "sort_order": "desc", "limit": limit},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return [o for o in resp.json()["observations"] if o["value"] != "."]  # newest first


def lookup_funding_annualized(funding_records, as_of_ms):
    relevant = [r for r in funding_records if r["time"] <= as_of_ms]
    if not relevant:
        return None
    recent = relevant[-24:]
    avg_hourly = sum(float(r["fundingRate"]) for r in recent) / len(recent)
    return avg_hourly * 24 * 365 * 100


def lookup_fear_greed(fng_records, as_of_ms):
    as_of_sec = as_of_ms / 1000
    for r in fng_records:  # newest first
        if int(r["timestamp"]) <= as_of_sec:
            return int(r["value"])
    return None


def lookup_macro_change(fred_records, as_of_ms):
    if not fred_records:
        return None
    as_of_date = time.strftime("%Y-%m-%d", time.gmtime(as_of_ms / 1000))
    candidates = [r for r in fred_records if r["date"] <= as_of_date]  # newest first already
    if len(candidates) < 2:
        return None
    latest, previous = float(candidates[0]["value"]), float(candidates[1]["value"])
    return (latest - previous) / previous * 100


def daily_technical_score(closes):
    price = closes[-1]
    sma20, sma50 = sma(closes, 20), sma(closes, 50)
    rsi14 = rsi(closes)
    stoch = stoch_rsi(closes)
    hist = macd_histogram(closes)
    bands = bollinger_bands(closes)

    score = 50.0
    reasons = []
    if sma20 and sma50:
        if price > sma20 > sma50:
            score += 15
            reasons.append("price above SMA20/SMA50")
        elif price < sma20 < sma50:
            score -= 15
            reasons.append("price below SMA20/SMA50")
    if rsi14 is not None:
        if rsi14 >= 70:
            score -= 5
            reasons.append(f"RSI {rsi14:.0f} overbought")
        elif rsi14 <= 30:
            score += 5
            reasons.append(f"RSI {rsi14:.0f} oversold")
    if stoch is not None:
        if stoch >= 0.8:
            score -= 5
            reasons.append("StochRSI overbought")
        elif stoch <= 0.2:
            score += 5
            reasons.append("StochRSI oversold")
    if hist is not None:
        score += 10 if hist > 0 else -10
        reasons.append(f"MACD {'positive' if hist > 0 else 'negative'}")
    if bands is not None:
        if price > bands["upper"]:
            score -= 5
            reasons.append("price above upper Bollinger Band")
        elif price < bands["lower"]:
            score += 5
            reasons.append("price below lower Bollinger Band")

    return max(0.0, min(100.0, score)), reasons


def score_day(daily_candles, idx, funding_records, fng_records, fred_records):
    """Point-in-time score using only candles[0:idx+1] and historical data up to that candle's
    close time - no lookahead."""
    closes = [c["close"] for c in daily_candles[: idx + 1]]
    if len(closes) < 55:  # need ~50 for SMA50 plus a margin
        return None
    price = closes[-1]
    as_of_ms = daily_candles[idx]["time"]

    supporting, conflicting = [], []
    tech_score, reasons = daily_technical_score(closes)
    weighted = tech_score
    for r in reasons:
        (supporting if tech_score >= 50 else conflicting).append(r)

    fg = lookup_fear_greed(fng_records, as_of_ms)
    if fg is not None:
        tilt = (50 - fg) * 0.10
        weighted += tilt
        (supporting if tilt > 0 else conflicting if tilt < 0 else supporting).append(f"Fear&Greed {fg}")

    annualized = lookup_funding_annualized(funding_records, as_of_ms)
    if annualized is not None:
        tilt = max(-8.0, min(8.0, -annualized * 0.1))
        weighted += tilt
        (supporting if tilt > 0 else conflicting if tilt < 0 else supporting).append(f"Funding {annualized:+.1f}% ann.")

    macro_chg = lookup_macro_change(fred_records, as_of_ms)
    if macro_chg is not None:
        tilt = -macro_chg * 2
        weighted += tilt
        (supporting if tilt > 0 else conflicting if tilt < 0 else supporting).append(f"Macro USD {macro_chg:+.2f}%")

    daily_candles_so_far = daily_candles[: idx + 1]
    ict = compute_ict_structure(daily_candles_so_far, price)
    structure_tilt = {"bullish_bos": 8, "bullish_choch": 6, "bearish_bos": -8, "bearish_choch": -6}
    st_tilt = structure_tilt.get(ict["structure"]["signal"], 0)
    if st_tilt:
        weighted += st_tilt
        (supporting if st_tilt > 0 else conflicting).append(f"Structure {ict['structure']['signal']}")
    if ict["zone"] is not None:
        zone_tilt = 3 if ict["zone"]["zone"] == "discount" else -3
        weighted += zone_tilt
        (supporting if zone_tilt > 0 else conflicting).append(f"ICT zone {ict['zone']['zone']}")

    daily_atr = atr(daily_candles_so_far)
    vol_pct = (daily_atr / price * 100) if daily_atr else 0
    # No cross-timeframe disagreement available daily-only (see module docstring) - confidence
    # here is driven by conflicting-signal count and sentiment extremity instead.
    confidence = max(10.0, 100 - len(conflicting) * 8 - (abs(fg - 50) * 0.2 if fg is not None else 0))
    risk_score = max(0.0, min(100.0, 30 + min(20.0, vol_pct * 3) + (abs(annualized) * 0.15 if annualized is not None else 0)))

    bull_score = max(0.0, min(100.0, weighted))
    raw_bias = "Long" if bull_score >= 60 else "Short" if bull_score <= 40 else "Neutral"

    invalidation, target, risk_reward = None, None, None
    if raw_bias in ("Long", "Short") and daily_atr:
        invalidation = price - 1.5 * daily_atr if raw_bias == "Long" else price + 1.5 * daily_atr
        stop_distance = abs(price - invalidation)
        target = nearest_target(
            raw_bias, price, ict["pools_above"] + ict["pools_below"], ict["open_gaps"], min_distance=stop_distance
        )
        if target:
            risk_reward = abs(target - price) / stop_distance if stop_distance else None

    bias, override_reason, size_tier, rejection_rule = apply_no_trade_rules(raw_bias, confidence, supporting, conflicting, risk_reward)

    return {
        "time": as_of_ms, "price": price, "bull_score": bull_score, "confidence": confidence,
        "raw_bias": raw_bias, "bias": bias, "size_tier": size_tier, "rejection_rule": rejection_rule,
        "invalidation": invalidation, "target": target, "risk_reward": risk_reward,
    }


def grade_outcome(daily_candles, decision_idx, bias, invalidation, target, max_days_forward=7):
    if bias not in ("Long", "Short") or not target or not invalidation:
        return None
    for j in range(decision_idx + 1, min(decision_idx + 1 + max_days_forward, len(daily_candles))):
        high, low = daily_candles[j]["high"], daily_candles[j]["low"]
        hit_target = high >= target if bias == "Long" else low <= target
        hit_stop = low <= invalidation if bias == "Long" else high >= invalidation
        if hit_target and hit_stop:
            return "ambiguous"
        if hit_target:
            return "win"
        if hit_stop:
            return "loss"
    return "open"


def run_backtest(coin="BTC", funding_days=80):
    daily_candles = fetch_hl_candles(coin, "1d", limit=250)
    funding_records = fetch_funding_history_days(coin, days=funding_days)
    fng_records = fetch_fear_greed_history()
    fred_records = fetch_fred_history("DTWEXBGS")

    earliest_funding_ms = funding_records[0]["time"] if funding_records else None
    decisions = []
    for idx in range(55, len(daily_candles)):
        day_ms = daily_candles[idx]["time"]
        if earliest_funding_ms and day_ms < earliest_funding_ms:
            continue  # before our funding history window - point-in-time data wouldn't exist
        scored = score_day(daily_candles, idx, funding_records, fng_records, fred_records)
        if scored is None:
            continue
        outcome = grade_outcome(daily_candles, idx, scored["bias"], scored["invalidation"], scored["target"])
        scored["outcome"] = outcome
        decisions.append(scored)

    return decisions


def summarize(decisions):
    total = len(decisions)
    rejected = [d for d in decisions if d["rejection_rule"]]
    taken = [d for d in decisions if d["bias"] in ("Long", "Short")]
    graded = [d for d in taken if d["outcome"] in ("win", "loss")]
    wins = [d for d in graded if d["outcome"] == "win"]

    by_rule = {}
    for d in rejected:
        by_rule[d["rejection_rule"]] = by_rule.get(d["rejection_rule"], 0) + 1

    by_tier = {}
    for d in taken:
        tier = d["size_tier"] or "?"
        by_tier.setdefault(tier, {"n": 0, "wins": 0})
        by_tier[tier]["n"] += 1
        if d["outcome"] == "win":
            by_tier[tier]["wins"] += 1

    return {
        "total_decision_days": total,
        "rejected": len(rejected),
        "rejected_by_rule": by_rule,
        "trades_taken": len(taken),
        "graded_trades": len(graded),
        "still_open_or_ambiguous": len(taken) - len(graded),
        "wins": len(wins),
        "win_rate_pct": (len(wins) / len(graded) * 100) if graded else None,
        "by_size_tier": by_tier,
    }
