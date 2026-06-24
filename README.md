# bitcoin-intel-agent

A minimal, free-to-run market intelligence tool for any coin listed on Hyperliquid (BTC,
ETH, SOL, HYPE, ...): pulls live data, fuses it into a probabilistic regime score, and
shows it as a report or a live-updating web dashboard. No paid APIs, no streaming
infrastructure, no database.

## Data sources (all free; only FRED needs a signup)

- **Price/technical** - Hyperliquid's own `candleSnapshot` API (1h/4h/1d candles, works for any coin listed there)
- **Perp funding/OI/volume** - [Hyperliquid public info API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
- **On-chain (BTC only)** - [mempool.space API](https://mempool.space/docs/api) (difficulty adjustment trend)
- **Sentiment** - [Alternative.me Fear & Greed Index](https://alternative.me/crypto/fear-and-greed-index/)
- **Macro (optional)** - [FRED API](https://fred.stlouisfed.org/docs/api/fred/) (broad USD index) - needs a free API key, see Setup below
- **News** - CoinDesk + CoinTelegraph RSS feeds, filtered by coin keyword where possible, falls back to general market headlines

## Setup

```
pip install -r requirements.txt
```

Optional - enable the macro (USD index) signal:

1. Create a free account at https://fred.stlouisfed.org and request an API key at
   https://fred.stlouisfed.org/docs/api/api_key.html (instant, no cost)
2. Set it as an environment variable before running:
   - PowerShell (current session): `$env:FRED_API_KEY = "your-key-here"`
   - PowerShell (persists across sessions): `setx FRED_API_KEY "your-key-here"` (open a new terminal after)

Without it, the report just prints "Macro: skipped" and everything else works as normal.

## Run

CLI, defaults to BTC:

```
python bitcoin_intel_agent.py
```

Live web dashboard (pick any coin, auto-refreshes):

```
streamlit run dashboard.py
```

Opens at http://localhost:8501. The refresh interval is capped at a 10-second minimum
in the sidebar to stay well within the free APIs' rate limits. The dashboard shows real
OHLC candlesticks with an Hour/Day/Week toggle (independent of the scoring timeframes
below it).

## How scoring works

Each timeframe (1h/4h/1d) gets a 0-100 score from price vs. moving averages, RSI,
Stochastic RSI, MACD momentum, and Bollinger Band extension, then the three are weighted
toward the daily chart. Fear & Greed is applied as a contrarian tilt (extreme fear nudges
the score up, extreme greed nudges it down) and also raises the Risk Score when it's at
an extreme. On-chain difficulty trend (BTC only) adds a small fundamental tilt.
Hyperliquid's funding rate gets the same contrarian treatment - crowded longs (positive
funding) nudge the score down, crowded shorts nudge it up, and extreme funding raises the
Risk Score. Daily ATR (volatility) feeds the Risk Score and sets the Invalidation level
(1.5x ATR from price). Confidence drops when timeframes disagree or sentiment is extreme.
All of this is transparent rule-based logic, not a trained ML model - see "Key Reasons" in
the output for exactly what drove each score.

On top of that, daily candles are run through some ICT (smart-money-concept) structure
analysis: swing-point liquidity pools (clusters of equal highs/lows - where resting stops
are assumed to sit), unfilled Fair Value Gaps, and Break of Structure / Change of Character
detection. A fresh break of structure feeds a small score tilt; the nearest liquidity pool
or FVG in your trade direction becomes the **Target** level (a concrete "when to take
profit" zone), alongside the existing ATR-based **Invalidation** ("when to cut losses")
level. Worth being honest about: ICT/SMC concepts are extremely popular among retail
traders now, which cuts against them being a hidden edge - treat the levels as useful
structure, not a guarantee.

Each run appends a row to `history.csv` (gitignored - it's your local run history, now
tagged per coin) so you can track Bull Score/Risk Score over time.

## Project layout

- `indicators.py` - pure technical-indicator math (SMA, EMA, RSI, StochRSI, MACD, Bollinger Bands, ATR), no I/O
- `ict.py` - pure ICT/smart-money structure math (swing points, liquidity pools, FVGs, market structure), no I/O
- `data_sources.py` - all the API calls
- `bitcoin_intel_agent.py` - fuses signals into a score; `run_analysis(coin)` is the reusable entry point
- `dashboard.py` - Streamlit live web UI on top of `run_analysis`, with real OHLC candlesticks

## Not in this version

Still no Kafka/streaming, no database, no ML model, no rigorous backtesting, no
wallet/whale tracking, no X/Twitter (no free API tier). This is a starting point to build
on, not the full system envisioned in the original research.

**This is not financial advice.** Bull/Risk scores are simple, transparent heuristics for
research and learning, not predictions, and this script never touches a wallet, exchange
account, or executes trades.
