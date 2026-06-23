"""Live web dashboard. Run with: streamlit run dashboard.py"""

import os

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

from bitcoin_intel_agent import HISTORY_FILE, log_run, run_analysis
from data_sources import fetch_hl_candles

COINS = ["BTC", "ETH", "SOL", "HYPE"]
CHART_INTERVALS = {"Hour": "1h", "Day": "1d", "Week": "1w"}

st.set_page_config(page_title="Hyperliquid Coin Intelligence", layout="wide")
st.title("Hyperliquid Coin Intelligence")
st.caption("Transparent rule-based heuristics, not financial advice. Never connects to a wallet or executes trades.")
st.caption("ICT/smart-money concepts are widely followed by retail traders too - treat as structure context, not a hidden edge.")

coin = st.sidebar.selectbox("Coin", COINS)
chart_label = st.sidebar.radio("Chart timeframe", list(CHART_INTERVALS.keys()), index=1, horizontal=True)
refresh_seconds = st.sidebar.slider("Refresh every (seconds)", min_value=10, max_value=120, value=30, step=5)
st.sidebar.caption("Kept at 10s minimum to stay well within the free APIs' rate limits.")


@st.fragment(run_every=f"{refresh_seconds}s")
def render(coin, chart_label):
    try:
        report = run_analysis(coin)
    except requests.exceptions.RequestException as exc:
        st.error(f"Data source unavailable: {exc}")
        return

    log_run(
        report["timestamp"], coin, report["price"], report["bull_score"], report["risk_score"],
        report["regime"], report["bias"], report["confidence"], report["fear_greed"],
    )

    row1 = st.columns(5)
    row1[0].metric("Bull Score", f"{report['bull_score']:.0f}/100")
    row1[1].metric("Risk Score", f"{report['risk_score']:.0f}/100")
    row1[2].metric("Regime", report["regime"])
    row1[3].metric("Trade Bias", report["bias"])
    row1[4].metric("Confidence", f"{report['confidence']:.0f}%")

    row2 = st.columns(4)
    row2[0].metric("Price", f"${report['price']:,.2f}")
    row2[1].metric("Open Interest", f"${report['open_interest_usd']:,.0f}")
    row2[2].metric("24h Volume", f"${report['day_volume_usd']:,.0f}")
    row2[3].metric("Fear & Greed", report["fear_greed"])

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

    levels = f"Support: ${report['support']:,.2f}  |  Resistance: ${report['resistance']:,.2f}"
    if report["invalidation"]:
        levels += f"  |  Invalidation: ${report['invalidation']:,.2f}"
    if report["target"]:
        levels += f"  |  Target: ${report['target']:,.2f}"
    st.write(levels)
    if report["ath"]:
        st.caption(f"All-time high: ${report['ath']:,.2f}  |  All-time low: ${report['atl']:,.2f}")

    st.subheader("Key Reasons")
    for reason in report["reasons"]:
        st.write(f"- {reason}")
    st.caption(f"Last updated {report['timestamp']}")

    if report["headlines"]:
        st.subheader("Recent Headlines")
        for h in report["headlines"]:
            tag = "(coin-specific)" if h["relevant"] else "(general market)"
            st.markdown(f"{tag} [{h['title']}]({h['link']})")

    st.divider()
    st.subheader(f"{coin} score history (this session)")
    if os.path.exists(HISTORY_FILE):
        hist_df = pd.read_csv(HISTORY_FILE)
        hist_df = hist_df[hist_df["coin"] == coin]
        if len(hist_df) > 1:
            hist_df["timestamp_utc"] = pd.to_datetime(hist_df["timestamp_utc"].str.replace(" UTC", ""))
            st.line_chart(hist_df.set_index("timestamp_utc")[["bull_score", "risk_score"]])
        else:
            st.caption("Not enough history yet for a chart - it builds up as this keeps refreshing.")


render(coin, chart_label)
