"""ICT/smart-money-concept structure analysis - pure math, no I/O.

Popular among retail traders, which cuts against it being a hidden edge - treat these
as structural context (good for setting entry/target/stop levels), not a proven signal.
"""


def find_swing_points(candles, lookback=3):
    """A swing high/low needs to be the extreme among `lookback` candles on each side."""
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    swing_highs, swing_lows = [], []
    for i in range(lookback, len(candles) - lookback):
        window_h = highs[i - lookback : i + lookback + 1]
        if highs[i] == max(window_h):
            swing_highs.append((i, highs[i]))
        window_l = lows[i - lookback : i + lookback + 1]
        if lows[i] == min(window_l):
            swing_lows.append((i, lows[i]))
    return swing_highs, swing_lows


def find_liquidity_pools(swing_points, tolerance_pct=0.5):
    """Clusters nearby equal highs/lows - the more touches, the more resting stops are assumed."""
    pools = []
    used = [False] * len(swing_points)
    for i, (_, price_i) in enumerate(swing_points):
        if used[i]:
            continue
        cluster = [price_i]
        used[i] = True
        for j in range(i + 1, len(swing_points)):
            if used[j]:
                continue
            _, price_j = swing_points[j]
            if abs(price_j - price_i) / price_i * 100 <= tolerance_pct:
                cluster.append(price_j)
                used[j] = True
        pools.append({"price": sum(cluster) / len(cluster), "touches": len(cluster)})
    return pools


def find_fair_value_gaps(candles):
    """3-candle imbalance: candle[i-2] and candle[i] don't overlap, candle[i-1] left a gap."""
    gaps = []
    for i in range(2, len(candles)):
        c0, c2 = candles[i - 2], candles[i]
        if c2["low"] > c0["high"]:
            gaps.append({"type": "bullish", "top": c2["low"], "bottom": c0["high"], "index": i})
        elif c2["high"] < c0["low"]:
            gaps.append({"type": "bearish", "top": c0["low"], "bottom": c2["high"], "index": i})
    return gaps


def unfilled_gaps(gaps, candles):
    """A gap is filled once price has traded back through its full range."""
    open_gaps = []
    for gap in gaps:
        filled = any(
            c["low"] <= gap["top"] and c["high"] >= gap["bottom"]
            for c in candles[gap["index"] + 1 :]
        )
        if not filled:
            open_gaps.append(gap)
    return open_gaps


def market_structure(swing_highs, swing_lows, current_price):
    """Break of Structure (trend continuation) vs Change of Character (potential reversal)."""
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {"trend": "undetermined", "signal": None}

    higher_high = swing_highs[-1][1] > swing_highs[-2][1]
    higher_low = swing_lows[-1][1] > swing_lows[-2][1]
    lower_high = swing_highs[-1][1] < swing_highs[-2][1]
    lower_low = swing_lows[-1][1] < swing_lows[-2][1]

    if higher_high and higher_low:
        trend = "uptrend"
    elif lower_high and lower_low:
        trend = "downtrend"
    else:
        trend = "ranging"

    last_high, last_low = swing_highs[-1][1], swing_lows[-1][1]
    signal = None
    if trend == "uptrend" and current_price < last_low:
        signal = "bearish_choch"
    elif trend == "downtrend" and current_price > last_high:
        signal = "bullish_choch"
    elif current_price > last_high:
        signal = "bullish_bos"
    elif current_price < last_low:
        signal = "bearish_bos"

    return {"trend": trend, "signal": signal, "last_swing_high": last_high, "last_swing_low": last_low}


def premium_discount_zone(current_price, range_high, range_low):
    if range_high <= range_low:
        return None
    pct = (current_price - range_low) / (range_high - range_low)
    return {"zone": "premium" if pct >= 0.5 else "discount", "pct_of_range": pct}


def nearest_target(bias, current_price, liquidity_pools, open_gaps, min_distance=0):
    """Nearest liquidity pool or FVG edge in the favorable direction, at least `min_distance`
    away - a natural take-profit zone. Without min_distance this picks the literal nearest
    level regardless of how close it is, which a 2026-06-24 backtest showed produces poor
    risk/reward almost always (avg R:R 0.27 across 36 rejected setups) since the stop is a
    fixed ATR multiple while the target had no minimum distance at all. Pass the stop
    distance as min_distance to guarantee R:R >= 1.0 by construction whenever a target is
    found, instead of measuring R:R after the fact and usually rejecting."""
    candidates = [p["price"] for p in liquidity_pools]
    candidates += [g["top"] if g["type"] == "bearish" else g["bottom"] for g in open_gaps]
    if bias == "Long":
        above = [p for p in candidates if p > current_price + min_distance]
        return min(above) if above else None
    if bias == "Short":
        below = [p for p in candidates if p < current_price - min_distance]
        return max(below) if below else None
    return None
