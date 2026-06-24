"""Bitcoin Intelligence Agent - free, mostly-keyless data sources only."""

import csv
import os
from datetime import datetime, timedelta, timezone

import requests

from data_sources import (
    fetch_fear_greed,
    fetch_fred_latest,
    fetch_hl_candles,
    fetch_hyperliquid_market_ctx,
    fetch_news_headlines,
    fetch_onchain_signal,
)
from ict import (
    find_fair_value_gaps,
    find_liquidity_pools,
    find_swing_points,
    market_structure,
    nearest_target,
    premium_discount_zone,
    unfilled_gaps,
)
from indicators import atr, bollinger_bands, macd_histogram, rsi, sma, stoch_rsi

TIMEFRAME_WEIGHTS = {"1h": 0.25, "4h": 0.35, "1d": 0.40}
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.csv")
FRED_DOLLAR_INDEX_SERIES = "DTWEXBGS"  # Nominal Broad U.S. Dollar Index


def analyze_timeframe(interval, coin):
    candles = fetch_hl_candles(coin, interval)
    closes = [c["close"] for c in candles]
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
            reasons.append(f"{interval}: price above SMA20/SMA50 (uptrend structure)")
        elif price < sma20 < sma50:
            score -= 15
            reasons.append(f"{interval}: price below SMA20/SMA50 (downtrend structure)")

    if rsi14 is not None:
        if rsi14 >= 70:
            score -= 5
            reasons.append(f"{interval}: RSI {rsi14:.0f} overbought")
        elif rsi14 <= 30:
            score += 5
            reasons.append(f"{interval}: RSI {rsi14:.0f} oversold")

    if stoch is not None:
        if stoch >= 0.8:
            score -= 5
            reasons.append(f"{interval}: StochRSI {stoch:.2f} overbought")
        elif stoch <= 0.2:
            score += 5
            reasons.append(f"{interval}: StochRSI {stoch:.2f} oversold")

    if hist is not None:
        score += 10 if hist > 0 else -10
        reasons.append(f"{interval}: MACD histogram {'positive' if hist > 0 else 'negative'}")

    if bands is not None:
        if price > bands["upper"]:
            score -= 5
            reasons.append(f"{interval}: price above upper Bollinger Band (extended)")
        elif price < bands["lower"]:
            score += 5
            reasons.append(f"{interval}: price below lower Bollinger Band (extended)")

    return {
        "interval": interval,
        "price": price,
        "score": max(0.0, min(100.0, score)),
        "reasons": reasons,
        "recent_high": max(c["high"] for c in candles[-30:]),
        "recent_low": min(c["low"] for c in candles[-30:]),
        "atr": atr(candles),
        "candles": candles,
    }


def compute_ict_structure(daily_candles, current_price):
    swing_highs, swing_lows = find_swing_points(daily_candles)
    open_gaps = unfilled_gaps(find_fair_value_gaps(daily_candles), daily_candles)
    structure = market_structure(swing_highs, swing_lows, current_price)

    zone = None
    if swing_highs and swing_lows:
        range_high = max(p[1] for p in swing_highs)
        range_low = min(p[1] for p in swing_lows)
        zone = premium_discount_zone(current_price, range_high, range_low)

    return {
        "structure": structure,
        "zone": zone,
        "pools_above": find_liquidity_pools(swing_highs),
        "pools_below": find_liquidity_pools(swing_lows),
        "open_gaps": open_gaps,
    }


def relative_strength_vs_btc(coin, daily_candles):
    """True relative-strength comparison (own 1d return vs BTC's, same window) - not a composite-score
    comparison, since BTC and the coin's own scores blend different inputs and aren't directly comparable."""
    if coin == "BTC" or len(daily_candles) < 2:
        return None
    btc_candles = fetch_hl_candles("BTC", "1d", limit=2)
    if len(btc_candles) < 2:
        return None
    coin_chg = (daily_candles[-1]["close"] - daily_candles[-2]["close"]) / daily_candles[-2]["close"] * 100
    btc_chg = (btc_candles[-1]["close"] - btc_candles[-2]["close"]) / btc_candles[-2]["close"] * 100
    return {"coin_change_pct": coin_chg, "btc_change_pct": btc_chg, "relative_pct": coin_chg - btc_chg}


def fuse(timeframe_results, fear_greed, onchain, funding, macro, ict, oi_baseline, rel_strength):
    signal_log = []  # {"text": ..., "contribution": float} - contribution drives supporting/conflicting later

    def add(contribution, text):
        signal_log.append({"text": text, "contribution": contribution})
        return contribution

    weighted_score = sum(r["score"] * TIMEFRAME_WEIGHTS[r["interval"]] for r in timeframe_results)
    risk_score = 30.0

    # Treated as a contrarian extremity signal (per conventional Fear & Greed usage),
    # not momentum: extreme fear nudges Bull Score up, extreme greed nudges it down.
    fg = fear_greed["value"]
    weighted_score += add((50 - fg) * 0.10, f"Sentiment: Fear & Greed Index at {fg} ({fear_greed['label']})")
    risk_score += abs(fg - 50) * 0.3

    if onchain is not None:
        diff_change = onchain["difficulty_change_pct"]
        weighted_score += add(
            3 if diff_change > 0 else -3,
            f"On-chain: next difficulty adjustment {diff_change:+.1f}% "
            f"(hashrate {'growing' if diff_change > 0 else 'declining'})",
        )

    # Crowded-long/short contrarian tilt, same idea as Fear & Greed, capped so it can't dominate.
    annualized = funding["annualized_pct"]
    weighted_score += add(
        max(-8.0, min(8.0, -annualized * 0.1)),
        f"Hyperliquid funding: {annualized:+.1f}% annualized "
        f"({'longs crowded, paying shorts' if annualized > 0 else 'shorts crowded, paying longs'}), "
        f"OI ${funding['open_interest_usd']:,.0f}, 24h vol ${funding['day_volume_usd']:,.0f}",
    )
    risk_score += min(15.0, abs(annualized) * 0.15)

    if macro is not None:
        # Broad dollar index as a risk-asset headwind/tailwind proxy: a stronger dollar
        # historically coincides with weaker risk-asset performance, and vice versa.
        weighted_score += add(-macro["change_pct"] * 2, f"Macro: broad USD index {macro['change_pct']:+.2f}% vs prior reading")
    else:
        signal_log.append({"text": "Macro: skipped (set FRED_API_KEY to enable)", "contribution": 0})

    daily = next(r for r in timeframe_results if r["interval"] == "1d")
    if daily["atr"]:
        vol_pct = daily["atr"] / daily["price"] * 100
        risk_score += min(20.0, vol_pct * 3)
        signal_log.append({"text": f"Volatility: daily ATR is {vol_pct:.1f}% of price", "contribution": 0})

    if oi_baseline is not None:
        price_chg = (daily["price"] - oi_baseline["price"]) / oi_baseline["price"] * 100
        oi_chg = (funding["open_interest_usd"] - oi_baseline["oi_usd"]) / oi_baseline["oi_usd"] * 100
        h = oi_baseline["hours_ago"]
        if oi_chg > 2 and price_chg > 0:
            weighted_score += add(4, f"OI/price: OI {oi_chg:+.1f}%, price {price_chg:+.1f}% over {h:.1f}h - trend backed by fresh positioning")
        elif oi_chg > 2 and price_chg < 0:
            risk_score += 5
            signal_log.append({
                "text": f"OI/price: OI {oi_chg:+.1f}% while price {price_chg:+.1f}% over {h:.1f}h - new positions building against price, squeeze risk either way",
                "contribution": 0,
            })
        elif oi_chg < -2 and price_chg > 0:
            weighted_score += add(-2, f"OI/price: OI {oi_chg:+.1f}% while price {price_chg:+.1f}% over {h:.1f}h - rally may be short-covering, not fresh conviction")
        elif oi_chg < -2 and price_chg < 0:
            weighted_score += add(2, f"OI/price: OI {oi_chg:+.1f}%, price {price_chg:+.1f}% over {h:.1f}h - de-leveraging selloff, downside may be exhausting")
        else:
            signal_log.append({"text": f"OI/price: roughly flat over {h:.1f}h, no strong divergence", "contribution": 0})
    else:
        signal_log.append({"text": "OI/price: not enough run history yet (builds up as this keeps running)", "contribution": 0})

    if rel_strength is not None:
        rel = rel_strength["relative_pct"]
        if rel > 2:
            weighted_score += add(
                3, f"Relative strength: {rel_strength['coin_change_pct']:+.1f}% vs BTC's "
                f"{rel_strength['btc_change_pct']:+.1f}% over 1d - outperforming the broader market",
            )
        elif rel < -2:
            weighted_score += add(
                -3, f"Relative strength: {rel_strength['coin_change_pct']:+.1f}% vs BTC's "
                f"{rel_strength['btc_change_pct']:+.1f}% over 1d - underperforming the broader market",
            )
        else:
            signal_log.append({
                "text": f"Relative strength: tracking BTC closely ({rel_strength['coin_change_pct']:+.1f}% vs {rel_strength['btc_change_pct']:+.1f}%)",
                "contribution": 0,
            })

    structure = ict["structure"]
    structure_text = {
        "bullish_bos": f"Structure: bullish break of structure above {structure.get('last_swing_high', 0):,.2f}",
        "bullish_choch": f"Structure: bullish change of character above {structure.get('last_swing_high', 0):,.2f}",
        "bearish_bos": f"Structure: bearish break of structure below {structure.get('last_swing_low', 0):,.2f}",
        "bearish_choch": f"Structure: bearish change of character below {structure.get('last_swing_low', 0):,.2f}",
    }
    structure_tilt = {"bullish_bos": 8, "bullish_choch": 6, "bearish_bos": -8, "bearish_choch": -6}
    if structure["signal"] in structure_tilt:
        weighted_score += add(structure_tilt[structure["signal"]], structure_text[structure["signal"]])
    else:
        signal_log.append({"text": f"Structure: {structure['trend']}, no fresh break of structure", "contribution": 0})

    if ict["zone"] is not None:
        zone_tilt = 3 if ict["zone"]["zone"] == "discount" else -3
        weighted_score += add(
            zone_tilt,
            f"ICT zone: price in {ict['zone']['zone']} ({ict['zone']['pct_of_range'] * 100:.0f}% of recent swing range)",
        )

    # Per-timeframe technical reasons: classify each timeframe as supporting or conflicting
    # by whether its own score leans the same direction as the final call (added below, once known).
    for tr in timeframe_results:
        tr_contribution = (tr["score"] - 50) * TIMEFRAME_WEIGHTS[tr["interval"]]
        for text in tr["reasons"]:
            signal_log.append({"text": text, "contribution": tr_contribution})

    scores = [r["score"] for r in timeframe_results]
    disagreement = max(scores) - min(scores)
    risk_score += disagreement * 0.3
    confidence = max(10.0, 100 - disagreement - abs(fg - 50) * 0.2)

    bull_score = max(0.0, min(100.0, weighted_score))
    risk_score = max(0.0, min(100.0, risk_score))

    reasons = [s["text"] for s in signal_log]
    direction = 1 if bull_score >= 50 else -1
    supporting = [s["text"] for s in signal_log if s["contribution"] * direction > 0]
    conflicting = [s["text"] for s in signal_log if s["contribution"] * direction < 0]

    return bull_score, risk_score, confidence, reasons, supporting, conflicting


def classify_regime(bull_score):
    if bull_score >= 80:
        return "Strong Bull"
    if bull_score >= 60:
        return "Bull"
    if bull_score > 40:
        return "Neutral"
    if bull_score > 20:
        return "Bear"
    return "Strong Bear"


def trade_bias(bull_score, risk_score):
    if risk_score >= 70:
        return "Neutral (reduce risk - high volatility/disagreement)"
    if bull_score >= 60:
        return "Long"
    if bull_score <= 40:
        return "Short"
    return "Neutral / await clarity"


def invalidation_level(daily, bias):
    if not daily["atr"]:
        return None
    if bias == "Long":
        return daily["price"] - 1.5 * daily["atr"]
    if bias == "Short":
        return daily["price"] + 1.5 * daily["atr"]
    return None


CONFIDENCE_TIERS = [(70.0, "Full"), (60.0, "Normal"), (50.0, "Small")]


def classify_confidence_tier(confidence):
    """Provisional thresholds (not yet backtested - there isn't enough logged history to
    tune them on yet). The tiering mechanism is sound regardless of where the exact cutoffs
    end up once review_recommendations() has enough graded history to validate against."""
    for threshold, tier in CONFIDENCE_TIERS:
        if confidence >= threshold:
            return tier
    return None


def apply_no_trade_rules(bias, confidence, supporting, conflicting, risk_reward):
    """A missed trade beats a bad trade - but instead of one hard confidence cutoff, weaker
    (still-positive) evidence gets a smaller position instead of being blocked outright.
    Conflicting signals and poor risk/reward still block entirely regardless of confidence -
    those are evidence-quality gates, not a frequency dial."""
    if bias not in ("Long", "Short"):
        return bias, None, None, None

    tier = classify_confidence_tier(confidence)
    if tier is None:
        return "No Trade", f"confidence {confidence:.0f}% is below the 50% minimum to act", None, "low_confidence"
    if len(conflicting) >= len(supporting):
        return (
            "No Trade", f"signals conflict ({len(supporting)} supporting vs {len(conflicting)} conflicting)",
            None, "signals_conflict",
        )
    if risk_reward is not None and risk_reward < 1.0:
        return "No Trade", f"risk/reward is {risk_reward:.2f} - risking more than the potential reward", None, "poor_risk_reward"
    return bias, None, tier, None


def log_run(timestamp, coin, daily_price, bull_score, risk_score, regime, bias, confidence, fg_value, oi_usd):
    is_new = not os.path.exists(HISTORY_FILE)
    with open(HISTORY_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(
                ["timestamp_utc", "coin", "price", "bull_score", "risk_score", "regime", "trade_bias", "confidence", "fear_greed", "open_interest_usd"]
            )
        writer.writerow(
            [timestamp, coin, f"{daily_price:.2f}", f"{bull_score:.1f}", f"{risk_score:.1f}", regime, bias, f"{confidence:.1f}", fg_value, f"{oi_usd:.2f}"]
        )


RECOMMENDATIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recommendations.csv")
REC_FIELDS = [
    "timestamp_utc", "coin", "final_bias", "raw_bias", "override_reason", "rejection_rule", "size_tier",
    "bull_score", "risk_score", "confidence", "entry_price", "stop_price", "target_price", "risk_reward",
    "supporting_signals", "conflicting_signals", "outcome", "outcome_checked_at",
]


def log_recommendation(timestamp, coin, final_bias, raw_bias, override_reason, rejection_rule, size_tier,
                        bull_score, risk_score, confidence, entry_price, stop_price, target_price,
                        risk_reward, supporting, conflicting):
    """Durable record for post-trade review - this is what future self-review reads from,
    not history.csv (which just tracks score trends, not individual recommendations).
    `rejection_rule` makes "how many were rejected, by which rule" directly queryable."""
    is_new = not os.path.exists(RECOMMENDATIONS_FILE)
    with open(RECOMMENDATIONS_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(REC_FIELDS)
        writer.writerow([
            timestamp, coin, final_bias, raw_bias, override_reason or "", rejection_rule or "", size_tier or "",
            f"{bull_score:.1f}", f"{risk_score:.1f}", f"{confidence:.1f}", f"{entry_price:.4f}",
            stop_price if stop_price is None else f"{stop_price:.4f}",
            target_price if target_price is None else f"{target_price:.4f}",
            risk_reward if risk_reward is None else f"{risk_reward:.2f}",
            " | ".join(supporting), " | ".join(conflicting), "", "",
        ])


def rejection_summary():
    """How many potential trades got declined, and by which specific rule - the actual data
    needed to answer "is any rule overly restrictive," instead of guessing."""
    if not os.path.exists(RECOMMENDATIONS_FILE):
        return {"total": 0, "rejected": 0, "by_rule": {}, "by_tier": {}}
    with open(RECOMMENDATIONS_FILE, newline="") as f:
        rows = list(csv.DictReader(f))

    by_rule, by_tier = {}, {}
    rejected = 0
    for row in rows:
        if row["rejection_rule"]:
            rejected += 1
            by_rule[row["rejection_rule"]] = by_rule.get(row["rejection_rule"], 0) + 1
        elif row["size_tier"]:
            by_tier[row["size_tier"]] = by_tier.get(row["size_tier"], 0) + 1
    return {"total": len(rows), "rejected": rejected, "by_rule": by_rule, "by_tier": by_tier}


def review_recommendations(coin, current_price, max_age_hours=168):
    """Grades past Long/Short recommendations that haven't been graded yet: did price hit the
    target, the stop, or neither so far? This is the actual data the self-improvement loop
    needs - without it, "adjust signal weighting" has nothing real to adjust from."""
    if not os.path.exists(RECOMMENDATIONS_FILE):
        return []
    rows = []
    with open(RECOMMENDATIONS_FILE, newline="") as f:
        rows = list(csv.DictReader(f))

    now = datetime.now(timezone.utc)
    results = []
    for row in rows:
        if row["coin"] != coin or row["final_bias"] not in ("Long", "Short") or row["outcome"]:
            continue
        rec_time = datetime.strptime(row["timestamp_utc"].replace(" UTC", ""), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        age_hours = (now - rec_time).total_seconds() / 3600
        if age_hours > max_age_hours:
            continue

        stop, target = float(row["stop_price"]), float(row["target_price"])
        if row["final_bias"] == "Long":
            outcome = "hit_target" if current_price >= target else "hit_stop" if current_price <= stop else "open"
        else:
            outcome = "hit_target" if current_price <= target else "hit_stop" if current_price >= stop else "open"
        results.append({**row, "computed_outcome": outcome, "age_hours": age_hours})
    return results


def find_oi_price_baseline(coin, target_hours_ago=24):
    """Closest history.csv row to `target_hours_ago` for this coin, or the oldest available if we
    don't have that much history yet. Returns None if there's no usable history at all."""
    if not os.path.exists(HISTORY_FILE):
        return None
    rows = []
    with open(HISTORY_FILE, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("coin") == coin and row.get("open_interest_usd"):
                rows.append(row)
    if not rows:
        return None

    now = datetime.now(timezone.utc)
    target = now - timedelta(hours=target_hours_ago)

    def parsed_time(row):
        return datetime.strptime(row["timestamp_utc"].replace(" UTC", ""), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)

    best = min(rows, key=lambda r: abs((parsed_time(r) - target).total_seconds()))
    hours_ago = (now - parsed_time(best)).total_seconds() / 3600
    return {"price": float(best["price"]), "oi_usd": float(best["open_interest_usd"]), "hours_ago": hours_ago}


def run_analysis(coin="BTC"):
    """Fetches all data and computes one report for `coin`. Raises requests.RequestException on hard failure."""
    timeframe_results = [analyze_timeframe(tf, coin) for tf in TIMEFRAME_WEIGHTS]
    fear_greed = fetch_fear_greed()
    onchain = fetch_onchain_signal() if coin == "BTC" else None
    funding = fetch_hyperliquid_market_ctx(coin)

    try:
        macro = fetch_fred_latest(FRED_DOLLAR_INDEX_SERIES)
    except requests.exceptions.RequestException:
        macro = None

    daily = next(r for r in timeframe_results if r["interval"] == "1d")
    hourly = next(r for r in timeframe_results if r["interval"] == "1h")
    ict = compute_ict_structure(daily["candles"], daily["price"])
    oi_baseline = find_oi_price_baseline(coin)

    try:
        rel_strength = relative_strength_vs_btc(coin, daily["candles"])
    except requests.exceptions.RequestException:
        rel_strength = None

    bull_score, risk_score, confidence, reasons, supporting, conflicting = fuse(
        timeframe_results, fear_greed, onchain, funding, macro, ict, oi_baseline, rel_strength
    )
    regime = classify_regime(bull_score)
    raw_bias = trade_bias(bull_score, risk_score)
    invalidation = invalidation_level(daily, raw_bias)
    stop_distance = abs(daily["price"] - invalidation) if invalidation else 0
    target = nearest_target(
        raw_bias, daily["price"], ict["pools_above"] + ict["pools_below"], ict["open_gaps"], min_distance=stop_distance
    )

    risk_reward = None
    if invalidation and target:
        risk_amt = abs(daily["price"] - invalidation)
        reward_amt = abs(target - daily["price"])
        risk_reward = reward_amt / risk_amt if risk_amt else None

    bias, override_reason, size_tier, rejection_rule = apply_no_trade_rules(raw_bias, confidence, supporting, conflicting, risk_reward)
    if override_reason:
        reasons.append(f"No Trade: {override_reason}")
    elif size_tier:
        reasons.append(f"Position sizing tier: {size_tier} ({confidence:.0f}% confidence)")

    try:
        weekly_candles = fetch_hl_candles(coin, "1w", limit=500)
        ath = max(c["high"] for c in weekly_candles)
        atl = min(c["low"] for c in weekly_candles)
    except requests.exceptions.RequestException:
        ath = atl = None

    try:
        headlines = fetch_news_headlines(coin)
    except requests.exceptions.RequestException:
        headlines = []

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if raw_bias in ("Long", "Short"):
        log_recommendation(
            timestamp, coin, bias, raw_bias, override_reason, rejection_rule, size_tier, bull_score, risk_score,
            confidence, daily["price"], invalidation, target, risk_reward, supporting, conflicting,
        )

    return {
        "coin": coin,
        "timestamp": timestamp,
        "price": daily["price"],
        "price_history": [(c["time"], c["close"]) for c in hourly["candles"]],
        "bull_score": bull_score,
        "risk_score": risk_score,
        "regime": regime,
        "bias": bias,
        "raw_bias": raw_bias,
        "override_reason": override_reason,
        "size_tier": size_tier,
        "confidence": confidence,
        "support": daily["recent_low"],
        "resistance": daily["recent_high"],
        "invalidation": invalidation,
        "target": target,
        "risk_reward": risk_reward,
        "supporting_signals": supporting,
        "conflicting_signals": conflicting,
        "ath": ath,
        "atl": atl,
        "open_interest_usd": funding["open_interest_usd"],
        "day_volume_usd": funding["day_volume_usd"],
        "fear_greed": fear_greed["value"],
        "reasons": reasons,
        "headlines": headlines,
    }


def main(coin="BTC"):
    try:
        report = run_analysis(coin)
    except requests.exceptions.RequestException as exc:
        print(f"Data source unavailable, aborting: {exc}")
        return

    print("=" * 60)
    print(f"{coin} INTELLIGENCE REPORT - {report['timestamp']}")
    print("=" * 60)
    print(f"Price: ${report['price']:,.2f}")
    print(f"Bull Score: {report['bull_score']:.0f}/100")
    print(f"Risk Score: {report['risk_score']:.0f}/100")
    print(f"Market Regime: {report['regime']}")
    print(f"Trade Bias: {report['bias']}" + (f" (raw call was {report['raw_bias']})" if report["override_reason"] else ""))
    if report["override_reason"]:
        print(f"  -> overridden to No Trade: {report['override_reason']}")
    elif report["size_tier"]:
        print(f"  -> Position size tier: {report['size_tier']}")
    print(f"Confidence: {report['confidence']:.0f}%")
    print()
    print(f"Open Interest: ${report['open_interest_usd']:,.0f}  |  24h Volume: ${report['day_volume_usd']:,.0f}")
    print(f"Support: ${report['support']:,.2f}  |  Resistance: ${report['resistance']:,.2f}")
    if report["invalidation"]:
        print(f"Invalidation (1.5x daily ATR): ${report['invalidation']:,.2f}")
    if report["target"]:
        print(f"Target (nearest liquidity/FVG): ${report['target']:,.2f}")
    if report["risk_reward"] is not None:
        print(f"Risk/Reward: {report['risk_reward']:.2f}")
    print(f"Signals: {len(report['supporting_signals'])} supporting, {len(report['conflicting_signals'])} conflicting")
    if report["ath"]:
        print(f"All-time high (since listing): ${report['ath']:,.2f}  |  All-time low: ${report['atl']:,.2f}")
    print()
    print("Key Reasons:")
    for reason in report["reasons"]:
        print(f"  - {reason}")
    if report["headlines"]:
        print()
        print("Recent Headlines:")
        for h in report["headlines"]:
            tag = "[coin-specific]" if h["relevant"] else "[general market]"
            when = h["published"].strftime("%b %d, %Y %H:%M UTC") if h["published"] else "date unknown"
            print(f"  {tag} ({when}) {h['title']}")
            print(f"      {h['link']}")
    print("=" * 60)

    log_run(
        report["timestamp"], coin, report["price"], report["bull_score"], report["risk_score"],
        report["regime"], report["bias"], report["confidence"], report["fear_greed"], report["open_interest_usd"],
    )


if __name__ == "__main__":
    main()
