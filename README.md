# bitcoin-intel-agent

A minimal, free-to-run version of the Bitcoin Intelligence Agent plan: pulls live data,
fuses it into a probabilistic regime score, and prints a structured report. No paid
APIs, no streaming infrastructure, no database - just a script you run on demand.

## Data sources (all free, no signup)

- **Price/technical** - [Binance public market data](https://data-api.binance.vision) (BTCUSDT klines, 1h/4h/1d)
- **On-chain** - [mempool.space API](https://mempool.space/docs/api) (difficulty adjustment trend)
- **Sentiment** - [Alternative.me Fear & Greed Index](https://alternative.me/crypto/fear-and-greed-index/)

## Setup

```
pip install -r requirements.txt
```

## Run

```
python bitcoin_intel_agent.py
```

## How scoring works

Each timeframe (1h/4h/1d) gets a 0-100 score from price vs. moving averages, RSI,
Stochastic RSI, MACD momentum, and Bollinger Band extension, then the three are weighted
toward the daily chart. Fear & Greed is applied as a contrarian tilt (extreme fear nudges
the score up, extreme greed nudges it down) and also raises the Risk Score when it's at
an extreme. On-chain difficulty trend adds a small fundamental tilt. Daily ATR (volatility)
feeds the Risk Score and sets the Invalidation level (1.5x ATR from price). Confidence
drops when timeframes disagree or sentiment is extreme. All of this is transparent
rule-based logic, not a trained ML model - see "Key Reasons" in the output for exactly
what drove each score.

Each run appends a row to `history.csv` (gitignored - it's your local run history) so
you can track Bull Score/Risk Score over time.

## Project layout

- `indicators.py` - pure math (SMA, EMA, RSI, StochRSI, MACD, Bollinger Bands, ATR), no I/O
- `data_sources.py` - all the API calls
- `bitcoin_intel_agent.py` - fuses signals into a score and prints/logs the report

## Not in this version

Compared to the original plan (`Executive Summary.pdf`): no Kafka/streaming, no
database, no social/news ingestion (X/Twitter has no free tier), no ML model, no
backtesting, no dashboard. This is a starting point to build on, not the full system.

**This is not financial advice.** Bull/Risk scores are simple, transparent heuristics for
research and learning, not predictions, and this script never touches a wallet, exchange
account, or executes trades.
