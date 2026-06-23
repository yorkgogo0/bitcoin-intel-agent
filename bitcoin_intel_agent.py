"""Bitcoin Intelligence Agent (MVP) - free, keyless data sources only."""

from datetime import datetime, timezone

import requests

BINANCE_BASE = "https://data-api.binance.vision/api/v3"
MEMPOOL_BASE = "https://mempool.space/api"
FNG_URL = "https://api.alternative.me/fng/"
REQUEST_TIMEOUT = 10

TIMEFRAME_WEIGHTS = {"1h": 0.25, "4h": 0.35, "1d": 0.40}


def fetch_klines(interval, limit=210):
    resp = requests.get(
        f"{BINANCE_BASE}/klines",
        params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return [
        {"open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4])}
        for row in resp.json()
    ]


def sma(values, period):
    return sum(values[-period:]) / period if len(values) >= period else None


def ema_series(values, period):
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    series = [sum(values[:period]) / period]
    for price in values[period:]:
        series.append(price * k + series[-1] * (1 - k))
    return series


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains = [max(values[i] - values[i - 1], 0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0) for i in range(1, len(values))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def macd_histogram(values, fast=12, slow=26, signal=9):
    if len(values) < slow + signal:
        return None
    ema_fast, ema_slow = ema_series(values, fast), ema_series(values, slow)
    offset = len(ema_fast) - len(ema_slow)
    macd_line = [f - s for f, s in zip(ema_fast[offset:], ema_slow)]
    signal_line = ema_series(macd_line, signal)
    return macd_line[-1] - signal_line[-1]


def analyze_timeframe(interval):
    candles = fetch_klines(interval)
    closes = [c["close"] for c in candles]
    price = closes[-1]
    sma20, sma50 = sma(closes, 20), sma(closes, 50)
    rsi14 = rsi(closes)
    hist = macd_histogram(closes)

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

    if hist is not None:
        score += 10 if hist > 0 else -10
        reasons.append(f"{interval}: MACD histogram {'positive' if hist > 0 else 'negative'}")

    return {
        "interval": interval,
        "price": price,
        "score": max(0.0, min(100.0, score)),
        "reasons": reasons,
        "recent_high": max(c["high"] for c in candles[-30:]),
        "recent_low": min(c["low"] for c in candles[-30:]),
    }


def fetch_fear_greed():
    resp = requests.get(FNG_URL, params={"limit": 1}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    entry = resp.json()["data"][0]
    return {"value": int(entry["value"]), "label": entry["value_classification"]}


def fetch_onchain_signal():
    diff = requests.get(f"{MEMPOOL_BASE}/v1/difficulty-adjustment", timeout=REQUEST_TIMEOUT)
    diff.raise_for_status()
    return {"difficulty_change_pct": diff.json()["difficultyChange"]}


def fuse(timeframe_results, fear_greed, onchain):
    weighted_score = sum(r["score"] * TIMEFRAME_WEIGHTS[r["interval"]] for r in timeframe_results)
    reasons = [r for tr in timeframe_results for r in tr["reasons"]]
    risk_score = 30.0

    # Treated as a contrarian extremity signal (per conventional Fear & Greed usage),
    # not momentum: extreme fear nudges Bull Score up, extreme greed nudges it down.
    fg = fear_greed["value"]
    weighted_score += (50 - fg) * 0.10
    risk_score += abs(fg - 50) * 0.3
    reasons.append(f"Sentiment: Fear & Greed Index at {fg} ({fear_greed['label']})")

    diff_change = onchain["difficulty_change_pct"]
    weighted_score += 3 if diff_change > 0 else -3
    reasons.append(
        f"On-chain: next difficulty adjustment {diff_change:+.1f}% "
        f"(hashrate {'growing' if diff_change > 0 else 'declining'})"
    )

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


def main():
    try:
        timeframe_results = [analyze_timeframe(tf) for tf in TIMEFRAME_WEIGHTS]
        fear_greed = fetch_fear_greed()
        onchain = fetch_onchain_signal()
    except requests.exceptions.RequestException as exc:
        print(f"Data source unavailable, aborting: {exc}")
        return

    bull_score, risk_score, confidence, reasons = fuse(timeframe_results, fear_greed, onchain)
    regime = classify_regime(bull_score)
    bias = trade_bias(bull_score, risk_score)

    daily = next(r for r in timeframe_results if r["interval"] == "1d")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print("=" * 60)
    print(f"BITCOIN INTELLIGENCE REPORT - {now}")
    print("=" * 60)
    print(f"Price: ${daily['price']:,.2f}")
    print(f"Bull Score: {bull_score:.0f}/100")
    print(f"Risk Score: {risk_score:.0f}/100")
    print(f"Market Regime: {regime}")
    print(f"Trade Bias: {bias}")
    print(f"Confidence: {confidence:.0f}%")
    print()
    print(f"Support: ${daily['recent_low']:,.2f}  |  Resistance: ${daily['recent_high']:,.2f}")
    print()
    print("Key Reasons:")
    for reason in reasons:
        print(f"  - {reason}")
    print("=" * 60)


if __name__ == "__main__":
    main()
