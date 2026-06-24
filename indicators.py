"""Pure technical-indicator math - no I/O, no network calls."""


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


def rsi_series(values, period=14):
    if len(values) < period + 1:
        return []
    gains = [max(values[i] - values[i - 1], 0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0) for i in range(1, len(values))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    series = [100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))]
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        series.append(100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss)))
    return series


def rsi(values, period=14):
    series = rsi_series(values, period)
    return series[-1] if series else None


def stoch_rsi(values, rsi_period=14, stoch_period=14):
    series = rsi_series(values, rsi_period)
    if len(series) < stoch_period:
        return None
    window = series[-stoch_period:]
    lo, hi = min(window), max(window)
    if hi == lo:
        return 0.5
    return (window[-1] - lo) / (hi - lo)


def macd_histogram(values, fast=12, slow=26, signal=9):
    if len(values) < slow + signal:
        return None
    ema_fast, ema_slow = ema_series(values, fast), ema_series(values, slow)
    offset = len(ema_fast) - len(ema_slow)
    macd_line = [f - s for f, s in zip(ema_fast[offset:], ema_slow)]
    signal_line = ema_series(macd_line, signal)
    return macd_line[-1] - signal_line[-1]


def bollinger_bands(values, period=20, num_std=2):
    if len(values) < period:
        return None
    window = values[-period:]
    mid = sum(window) / period
    variance = sum((v - mid) ** 2 for v in window) / period
    std = variance ** 0.5
    return {"lower": mid - num_std * std, "mid": mid, "upper": mid + num_std * std}


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        high, low, prev_close = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    avg = sum(trs[:period]) / period
    for tr in trs[period:]:
        avg = (avg * (period - 1) + tr) / period
    return avg
