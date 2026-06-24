"""Live web dashboard. Run with: streamlit run dashboard.py"""

import os

import pandas as pd
import requests
import streamlit as st

from bitcoin_intel_agent import HISTORY_FILE, log_run, run_analysis

COINS = ["BTC", "ETH", "SOL", "HYPE"]

st.set_page_config(page_title="Hyperliquid Coin Intelligence", layout="wide")
st.title("Hyperliquid Coin Intelligence")
st.caption("Transparent rule-based heuristics, not financial advice. Never connects to a wallet or executes trades.")

coin = st.sidebar.selectbox("Coin", COINS)
refresh_seconds = st.sidebar.slider("Refresh every (seconds)", min_value=10, max_value=120, value=30, step=5)
st.sidebar.caption("Kept at 10s minimum to stay well within the free APIs' rate limits.")


@st.fragment(run_every=f"{refresh_seconds}s")
def render(coin):
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

    price_df = pd.DataFrame(report["price_history"], columns=["time", "price"])
    price_df["time"] = pd.to_datetime(price_df["time"], unit="ms")
    st.subheader(f"{coin} price (hourly, last ~9 days)")
    st.line_chart(price_df.set_index("time")["price"])

    levels = f"Support: ${report['support']:,.2f}  |  Resistance: ${report['resistance']:,.2f}"
    if report["invalidation"]:
        levels += f"  |  Invalidation: ${report['invalidation']:,.2f}"
    st.write(levels)

    st.subheader("Key Reasons")
    for reason in report["reasons"]:
        st.write(f"- {reason}")
    st.caption(f"Last updated {report['timestamp']}")

    st.divider()
    st.subheader(f"{coin} history (this session)")
    if os.path.exists(HISTORY_FILE):
        df = pd.read_csv(HISTORY_FILE)
        df = df[df["coin"] == coin]
        if len(df) > 1:
            df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"].str.replace(" UTC", ""))
            st.line_chart(df.set_index("timestamp_utc")[["bull_score", "risk_score"]])
        else:
            st.caption("Not enough history yet for a chart - it builds up as this keeps refreshing.")


render(coin)
