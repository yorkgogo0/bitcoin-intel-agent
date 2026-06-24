"""Live web dashboard. Run with: streamlit run dashboard.py"""

import os

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

from bitcoin_intel_agent import HISTORY_FILE, log_run, run_analysis
from data_sources import fetch_hl_candles, fetch_wallet_state

COINS = ["BTC", "ETH", "SOL", "HYPE"]
CHART_INTERVALS = {"Hour": "1h", "Day": "1d", "Week": "1w"}

BULL_STEPS = [  # mirrors classify_regime's own thresholds
    {"range": [0, 20], "color": "#dc2626"},
    {"range": [20, 40], "color": "#f87171"},
    {"range": [40, 60], "color": "#fbbf24"},
    {"range": [60, 80], "color": "#86efac"},
    {"range": [80, 100], "color": "#16a34a"},
]
RISK_STEPS = [{"range": [0, 30], "color": "#22c55e"}, {"range": [30, 60], "color": "#fbbf24"}, {"range": [60, 100], "color": "#dc2626"}]
CONFIDENCE_STEPS = [{"range": [0, 40], "color": "#dc2626"}, {"range": [40, 70], "color": "#fbbf24"}, {"range": [70, 100], "color": "#22c55e"}]
FEAR_GREED_STEPS = [  # matches the conventional Fear & Greed gauge: red=fear, green=greed
    {"range": [0, 25], "color": "#dc2626"},
    {"range": [25, 45], "color": "#f97316"},
    {"range": [45, 55], "color": "#fbbf24"},
    {"range": [55, 75], "color": "#84cc16"},
    {"range": [75, 100], "color": "#16a34a"},
]


def make_gauge(title, value, steps, suffix=""):
    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=value,
            title={"text": title, "font": {"size": 15}},
            number={"font": {"size": 30}, "suffix": suffix},
            gauge={
                "axis": {"range": [0, 100], "tickwidth": 1},
                "bar": {"color": "rgba(0,0,0,0)", "thickness": 0},
                "bgcolor": "rgba(0,0,0,0)",
                "steps": steps,
                "threshold": {"line": {"color": "white", "width": 5}, "thickness": 0.9, "value": value},
            },
        )
    )
    fig.update_layout(height=200, margin=dict(l=20, r=20, t=45, b=10), paper_bgcolor="rgba(0,0,0,0)")
    return fig


def colored_box(container, label, value, good, bad):
    """good/bad are sets of values that should render green/red; anything else is yellow."""
    if value in good:
        container.success(f"**{label}:** {value}")
    elif value in bad:
        container.error(f"**{label}:** {value}")
    else:
        container.warning(f"**{label}:** {value}")


st.set_page_config(page_title="Hyperliquid Coin Intelligence", layout="wide")
st.title("Hyperliquid Coin Intelligence")
st.caption("Transparent rule-based heuristics, not financial advice. Never connects to a wallet or executes trades.")
st.caption("ICT/smart-money concepts are widely followed by retail traders too - treat as structure context, not a hidden edge.")

coin = st.sidebar.selectbox("Coin", COINS)
chart_label = st.sidebar.radio("Chart timeframe", list(CHART_INTERVALS.keys()), index=1, horizontal=True)
refresh_seconds = st.sidebar.slider("Refresh every (seconds)", min_value=10, max_value=120, value=30, step=5)
st.sidebar.caption("Kept at 10s minimum to stay well within the free APIs' rate limits.")

st.sidebar.divider()
watch_input = st.sidebar.text_area("Track wallets (one address per line)", height=100)
st.sidebar.caption("Read-only - public position data for any address. Doesn't connect a wallet or place trades.")
watch_addresses = [a.strip() for a in watch_input.splitlines() if a.strip()]


@st.fragment(run_every=f"{refresh_seconds}s")
def render(coin, chart_label):
    try:
        report = run_analysis(coin)
    except requests.exceptions.RequestException as exc:
        st.error(f"Data source unavailable: {exc}")
        return

    log_run(
        report["timestamp"], coin, report["price"], report["bull_score"], report["risk_score"],
        report["regime"], report["bias"], report["confidence"], report["fear_greed"], report["open_interest_usd"],
    )

    gauges = st.columns(4)
    gauges[0].plotly_chart(make_gauge("Bull Score", report["bull_score"], BULL_STEPS), use_container_width=True)
    gauges[1].plotly_chart(make_gauge("Risk Score", report["risk_score"], RISK_STEPS), use_container_width=True)
    gauges[2].plotly_chart(make_gauge("Confidence", report["confidence"], CONFIDENCE_STEPS, suffix="%"), use_container_width=True)
    gauges[3].plotly_chart(make_gauge("Fear & Greed", report["fear_greed"], FEAR_GREED_STEPS), use_container_width=True)

    badges = st.columns(2)
    colored_box(badges[0], "Market Regime", report["regime"], good={"Bull", "Strong Bull"}, bad={"Bear", "Strong Bear"})
    colored_box(badges[1], "Trade Bias", report["bias"], good={"Long"}, bad={"Short"})
    if report["override_reason"]:
        st.info(f"Raw call was **{report['raw_bias']}**, overridden to **No Trade**: {report['override_reason']}")

    row2 = st.columns(3)
    row2[0].metric("Price", f"${report['price']:,.2f}")
    row2[1].metric("Open Interest", f"${report['open_interest_usd']:,.0f}")
    row2[2].metric("24h Volume", f"${report['day_volume_usd']:,.0f}")

    interval = CHART_INTERVALS[chart_label]
    try:
        candles = fetch_hl_candles(coin, interval, limit=150)
        candle_df = pd.DataFrame(candles)
        candle_df["time"] = pd.to_datetime(candle_df["time"], unit="ms")
        fig = go.Figure(
            data=[
                go.Candlestick(
                    x=candle_df["time"],
                    open=candle_df["open"],
                    high=candle_df["high"],
                    low=candle_df["low"],
                    close=candle_df["close"],
                    increasing_line_color="#16a34a",
                    decreasing_line_color="#dc2626",
                )
            ]
        )
        fig.update_layout(
            title=f"{coin} candles ({chart_label.lower()})",
            xaxis_rangeslider_visible=False,
            margin=dict(l=10, r=10, t=40, b=10),
            height=420,
        )
        st.plotly_chart(fig, use_container_width=True)
    except requests.exceptions.RequestException as exc:
        st.warning(f"Couldn't load candle chart: {exc}")

    levels = st.columns(4)
    levels[0].metric(
        "Support", f"${report['support']:,.2f}",
        help="Price floor based on the recent 30-day low - where buying has historically been strong enough to stop declines.",
    )
    levels[1].metric(
        "Resistance", f"${report['resistance']:,.2f}",
        help="Price ceiling based on the recent 30-day high - where selling has historically been strong enough to stop rallies.",
    )
    levels[2].metric(
        "Invalidation", f"${report['invalidation']:,.2f}" if report["invalidation"] else "-",
        help="Your stop-loss: if price reaches here, the current Long/Short thesis is considered wrong. Set at 1.5x the daily ATR (volatility) away from price.",
    )
    levels[3].metric(
        "Target", f"${report['target']:,.2f}" if report["target"] else "-",
        help="Your take-profit: the nearest ICT liquidity pool or Fair Value Gap in the trade's favorable direction, where price is likely to react.",
    )
    if report["ath"]:
        st.caption(f"All-time high: ${report['ath']:,.2f}  |  All-time low: ${report['atl']:,.2f}")
    if report["risk_reward"] is not None:
        st.caption(f"Risk/Reward: {report['risk_reward']:.2f}" + (" - below 1.0, risking more than the reward" if report["risk_reward"] < 1 else ""))

    sig_cols = st.columns(2)
    with sig_cols[0]:
        st.subheader(f"Supporting ({len(report['supporting_signals'])})")
        for s in report["supporting_signals"]:
            st.write(f"- {s}")
    with sig_cols[1]:
        st.subheader(f"Conflicting ({len(report['conflicting_signals'])})")
        for s in report["conflicting_signals"]:
            st.write(f"- {s}")

    st.subheader("Key Reasons")
    for reason in report["reasons"]:
        st.write(f"- {reason}")
    st.caption(f"Last updated {report['timestamp']}")

    if report["headlines"]:
        st.subheader("Recent Headlines")
        for h in report["headlines"]:
            tag = "coin-specific" if h["relevant"] else "general market"
            when = h["published"].strftime("%b %d, %Y %H:%M UTC") if h["published"] else "date unknown"
            st.markdown(f"**{when}** ({tag}) - [{h['title']}]({h['link']})")

    st.divider()
    st.subheader(f"{coin} score history (this session)")
    if os.path.exists(HISTORY_FILE):
        hist_df = pd.read_csv(HISTORY_FILE)
        hist_df = hist_df[hist_df["coin"] == coin]
        if len(hist_df) > 1:
            hist_df["timestamp_utc"] = pd.to_datetime(hist_df["timestamp_utc"].str.replace(" UTC", ""))
            hist_fig = go.Figure()
            hist_fig.add_trace(go.Scatter(x=hist_df["timestamp_utc"], y=hist_df["bull_score"], name="Bull Score", line={"color": "#16a34a"}))
            hist_fig.add_trace(go.Scatter(x=hist_df["timestamp_utc"], y=hist_df["risk_score"], name="Risk Score", line={"color": "#dc2626"}))
            hist_fig.update_layout(height=300, margin=dict(l=10, r=10, t=20, b=10))
            st.plotly_chart(hist_fig, use_container_width=True)
        else:
            st.caption("Not enough history yet for a chart - it builds up as this keeps refreshing.")


render(coin, chart_label)


@st.fragment(run_every=f"{refresh_seconds}s")
def render_watchlist(addresses):
    if not addresses:
        return
    st.divider()
    st.subheader("Whale Watchlist")
    for address in addresses:
        try:
            wallet = fetch_wallet_state(address)
        except requests.exceptions.RequestException as exc:
            st.warning(f"{address}: couldn't load ({exc})")
            continue

        total_pnl = sum(p["unrealized_pnl"] for p in wallet["positions"])
        label = f"{address}  -  account value ${wallet['account_value_usd']:,.0f}  -  unrealized P&L ${total_pnl:,.0f}"
        with st.expander(label, expanded=True):
            if total_pnl > 0:
                st.success(f"Net unrealized P&L: +${total_pnl:,.0f}")
            elif total_pnl < 0:
                st.error(f"Net unrealized P&L: -${abs(total_pnl):,.0f}")
            if wallet["positions"]:
                pos_df = pd.DataFrame(wallet["positions"])
                st.dataframe(pos_df, use_container_width=True, hide_index=True)
            else:
                st.caption("No open positions right now.")


render_watchlist(watch_addresses)
