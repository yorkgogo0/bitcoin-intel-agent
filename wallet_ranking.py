"""Ranks a list of wallet addresses by real historical performance, using Hyperliquid's
free public `portfolio` and `clearinghouseState` endpoints - no key needed.

Ranks by PnL-per-dollar-of-volume-traded rather than raw PnL or account-value-based ROI:
raw PnL favors big accounts regardless of skill, and the portfolio endpoint's starting
accountValueHistory point is often 0 (a tracking-start artifact, not a real deposit), which
makes volume-normalized PnL a more stable comparator across wallets of very different sizes.
"""

import requests

from data_sources import HYPERLIQUID_INFO_URL, REQUEST_TIMEOUT, fetch_wallet_state


def fetch_portfolio(address):
    resp = requests.post(HYPERLIQUID_INFO_URL, json={"type": "portfolio", "user": address}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return dict(resp.json())


def _latest_pnl(period_data):
    history = period_data.get("pnlHistory") or []
    return float(history[-1][1]) if history else 0.0


def characterize_style(wallet_state):
    positions = wallet_state["positions"]
    if not positions:
        return "no open positions right now"

    n = len(positions)
    avg_leverage = sum(p["leverage"] for p in positions) / n
    longs = sum(1 for p in positions if p["side"] == "Long")
    bias = "long-biased" if longs > n / 2 else "short-biased" if longs < n / 2 else "balanced"
    deployed = sum(p["position_value_usd"] for p in positions)
    concentration_pct = deployed / wallet_state["account_value_usd"] * 100 if wallet_state["account_value_usd"] else 0

    if avg_leverage >= 20:
        risk_label = "high-leverage"
    elif avg_leverage >= 8:
        risk_label = "moderate-leverage"
    else:
        risk_label = "low-leverage"

    return f"{n} open position(s), {risk_label} (avg {avg_leverage:.0f}x), {bias}, {concentration_pct:.0f}% of equity deployed"


def rank_wallets(addresses):
    results = []
    for addr in addresses:
        try:
            portfolio = fetch_portfolio(addr)
            wallet_state = fetch_wallet_state(addr)
        except requests.exceptions.RequestException as exc:
            results.append({"address": addr, "error": str(exc)})
            continue

        all_time, week, month = portfolio["allTime"], portfolio["week"], portfolio["month"]
        all_time_pnl = _latest_pnl(all_time)
        all_time_vlm = float(all_time.get("vlm") or 0.0)
        pnl_per_volume_pct = (all_time_pnl / all_time_vlm * 100) if all_time_vlm else None

        results.append({
            "address": addr,
            "account_value_usd": wallet_state["account_value_usd"],
            "all_time_pnl": all_time_pnl,
            "all_time_volume": all_time_vlm,
            "pnl_per_volume_pct": pnl_per_volume_pct,
            "week_pnl": _latest_pnl(week),
            "month_pnl": _latest_pnl(month),
            "open_positions": len(wallet_state["positions"]),
            "style": characterize_style(wallet_state),
        })
    return results
