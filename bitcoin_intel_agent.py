"""Bitcoin Intelligence Agent - free, mostly-keyless data sources only."""

import csv
import os
from datetime import datetime, timezone

import requests

from data_sources import (
    fetch_fear_greed,
    fetch_fred_latest,
    fetch_hl_candles,
    fetch_hyperliquid_market_ctx,
    fetch_onchain_signal,
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
    }


def fuse(timeframe_results, fear_greed, onchain, funding, macro):
    weighted_score = sum(r["score"] * TIMEFRAME_WEIGHTS[r["interval"]] for r in timeframe_results)
    reasons = [r for tr in timeframe_results for r in tr["reasons"]]
    risk_score = 30.0

    # Treated as a contrarian extremity signal (per conventional Fear & Greed usage),
    # not momentum: extreme fear nudges Bull Score up, extreme greed nudges it down.
    fg = fear_greed["value"]
    weighted_score += (50 - fg) * 0.10
    risk_score += abs(fg - 50) * 0.3
    reasons.append(f"Sentiment: Fear & Greed Index at {fg} ({fear_greed['label']})")

    if onchain is not None:
        diff_change = onchain["difficulty_change_pct"]
        weighted_score += 3 if diff_change > 0 else -3
        reasons.append(
            f"On-chain: next difficulty adjustment {diff_change:+.1f}% "
            f"(hashrate {'growing' if diff_change > 0 else 'declining'})"
        )

    # Crowded-long/short contrarian tilt, same idea as Fear & Greed, capped so it can't dominate.
    annualized = funding["annualized_pct"]
    weighted_score += max(-8.0, min(8.0, -annualized * 0.1))
    risk_score += min(15.0, abs(annualized) * 0.15)
    reasons.append(
        f"Hyperliquid funding: {annualized:+.1f}% annualized "
        f"({'longs crowded, paying shorts' if annualized > 0 else 'shorts crowded, paying longs'}), "
        f"OI ${funding['open_interest_usd']:,.0f}, 24h vol ${funding['day_volume_usd']:,.0f}"
    )

    if macro is not None:
        # Broad dollar index as a risk-asset headwind/tailwind proxy: a stronger dollar
        # historically coincides with weaker risk-asset performance, and vice versa.
        weighted_score += -macro["change_pct"] * 2
        reasons.append(f"Macro: broad USD index {macro['change_pct']:+.2f}% vs prior reading")
    else:
        reasons.append("Macro: skipped (set FRED_API_KEY to enable)")

    daily = next(r for r in timeframe_results if r["interval"] == "1d")
    if daily["atr"]:
        vol_pct = daily["atr"] / daily["price"] * 100
        risk_score += min(20.0, vol_pct * 3)
        reasons.append(f"Volatility: daily ATR is {vol_pct:.1f}% of price")

    scores = [r["score"] for r in timeframe_results]
    disagreement = max(scores) - min(scores)
    risk_score += disagreement * 0.3
    confidence = max(10.0, 100 - disagreement - abs(fg - 50) * 0.2)

    bull_score = max(0.0, min(100.0, weighted_score))
    risk_score = max(0.0, min(100.0, risk_score))
    return bull_score, risk_score, confidence, reasons


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


def log_run(timestamp, coin, daily_price, bull_score, risk_score, regime, bias, confidence, fg_value):
    is_new = not os.path.exists(HISTORY_FILE)
    with open(HISTORY_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(
                ["timestamp_utc", "coin", "price", "bull_score", "risk_score", "regime", "trade_bias", "confidence", "fear_greed"]
            )
        writer.writerow(
            [timestamp, coin, f"{daily_price:.2f}", f"{bull_score:.1f}", f"{risk_score:.1f}", regime, bias, f"{confidence:.1f}", fg_value]
        )


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

    bull_score, risk_score, confidence, reasons = fuse(timeframe_results, fear_greed, onchain, funding, macro)
    regime = classify_regime(bull_score)
    bias = trade_bias(bull_score, risk_score)
    daily = next(r for r in timeframe_results if r["interval"] == "1d")
    invalidation = invalidation_level(daily, bias)

    return {
        "coin": coin,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "price": daily["price"],
        "bull_score": bull_score,
        "risk_score": risk_score,
        "regime": regime,
        "bias": bias,
        "confidence": confidence,
        "support": daily["recent_low"],
        "resistance": daily["recent_high"],
        "invalidation": invalidation,
        "open_interest_usd": funding["open_interest_usd"],
        "day_volume_usd": funding["day_volume_usd"],
        "fear_greed": fear_greed["value"],
        "reasons": reasons,
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
    print(f"Trade Bias: {report['bias']}")
    print(f"Confidence: {report['confidence']:.0f}%")
    print()
    print(f"Open Interest: ${report['open_interest_usd']:,.0f}  |  24h Volume: ${report['day_volume_usd']:,.0f}")
    print(f"Support: ${report['support']:,.2f}  |  Resistance: ${report['resistance']:,.2f}")
    if report["invalidation"]:
        print(f"Invalidation (1.5x daily ATR): ${report['invalidation']:,.2f}")
    print()
    print("Key Reasons:")
    for reason in report["reasons"]:
        print(f"  - {reason}")
    print("=" * 60)

    log_run(
        report["timestamp"], coin, report["price"], report["bull_score"], report["risk_score"],
        report["regime"], report["bias"], report["confidence"], report["fear_greed"],
    )


if __name__ == "__main__":
    main()
