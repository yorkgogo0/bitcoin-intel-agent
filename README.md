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

For non-BTC coins, a **relative-strength check** compares the coin's own 1-day return to
BTC's 1-day return over the same window (true relative strength - not a comparison of
composite scores, which blend different inputs and aren't directly comparable). Tracking
BTC closely is neutral; meaningfully outperforming or underperforming it is a real,
cheap-to-compute divergence signal. Open interest is also now tracked over time (logged
to `history.csv`) so each run compares current OI to ~24h ago against price's own move
over the same window - rising OI with rising price reads as trend confirmation; rising OI
against falling price reads as a crowd building against price (squeeze risk either way).

Each run appends a row to `history.csv` (gitignored - it's your local run history, now
tagged per coin) so you can track Bull Score/Risk Score/OI over time.

## No-trade rules and tiered position sizing

Every signal that contributes to the Bull Score is tagged as **supporting** or
**conflicting** relative to the final call, not just dumped into one flat list. A raw
Long/Short call gets a **size tier** instead of a single pass/fail cutoff:

- Below 50% confidence: blocked entirely (**No Trade**)
- 50-59%: **Small** size tier
- 60-69%: **Normal** size tier
- 70%+: **Full** size tier

Regardless of confidence, a call is still blocked entirely (**No Trade**) if conflicting
signals outnumber (or tie) supporting signals, or if Risk/Reward is below 1.0 - those are
evidence-quality gates, not a frequency dial. "Do not force trades when evidence is weak"
applies to those, not to confidence level itself.

**Honest caveat:** the exact tier cutoffs (50/60/70) are a reasonable starting point, not a
backtested result - there isn't enough logged history yet to validate them against. Treat
them as provisional until `review_recommendations()` has enough graded history to tune
against.

Every Long/Short call (whether or not it survives the no-trade filter) gets logged to
`recommendations.csv` (gitignored) with the full reasoning, confidence, size tier, entry/
stop/target, risk/reward, and the supporting/conflicting signal lists - a durable record
for reviewing later. `review_recommendations(coin, current_price)` grades past calls that
haven't been graded yet: did price hit the target, the stop, or is it still open.
`rejection_summary()` reports how many calls were rejected and by which specific rule. This
is the actual data a future signal-reweighting pass (or threshold tuning) would need -
there's no shortcut to it without a real logged history to review.

## Market Scanner

A lighter-weight scan across the top-volume Hyperliquid assets (not just BTC/ETH/SOL/HYPE),
in its own dashboard menu. Deliberately capped to the top ~25-50 assets by 24h volume, not
all ~230 tradable perps: the top 25 capture roughly 97-98% of total trading volume on
Hyperliquid, and the long tail includes literally zero-volume markets where a "score" would
be noise, not signal. `screener.py` computes a cheaper version of the score (technical +
funding + ICT structure only - no news/macro/on-chain/relative-strength/OI-trend, since
those need per-coin config or accumulated history that doesn't scale across many assets) so
scanning ~25 assets takes under a minute instead of running the full pipeline ~25 times.
Filter/sort presets include highest-confidence longs/shorts, strong structure signals,
unusual funding, low-risk setups, and high-conviction setups. Treat a strong scanner result
as a reason to open the full Coin Analysis view for that asset, not as a final answer by
itself - it's missing several of the signals the full analysis has.

## Whale Watchlist

The dashboard has a sidebar field to paste wallet addresses (one per line). For each one
it shows current account value, open positions, leverage, entry price, unrealized P&L, and
liquidation price - all from Hyperliquid's own free public API (`clearinghouseState`), no
key needed, since position data for any address is public. This is **read-only**: it never
connects a wallet or places trades. Finding *which* wallets are worth watching is a
separate problem this tool doesn't solve - [HyperTracker](https://hypertracker.io) has a
free-tier API (100 requests/day, no card) with a real leaderboard if you want a starting
list.

## Project layout

- `indicators.py` - pure technical-indicator math (SMA, EMA, RSI, StochRSI, MACD, Bollinger Bands, ATR), no I/O
- `ict.py` - pure ICT/smart-money structure math (swing points, liquidity pools, FVGs, market structure), no I/O
- `data_sources.py` - all the API calls, including the free wallet/position lookup
- `bitcoin_intel_agent.py` - fuses signals into a score; `run_analysis(coin)` is the reusable entry point
- `screener.py` - lightweight multi-asset scan across the top-volume universe, reuses `compute_ict_structure` from `bitcoin_intel_agent.py`
- `dashboard.py` - Streamlit live web UI: Coin Analysis (full analysis + candlesticks + Whale Watchlist) and Market Scanner menus

## Not in this version

Still no Kafka/streaming, no database, no ML model, no rigorous backtesting, no
automated trade execution or copy-trading, no X/Twitter (no free API tier). This is a
starting point to build on, not the full system envisioned in the original research.

**This is not financial advice.** Bull/Risk scores are simple, transparent heuristics for
research and learning, not predictions, and this script never touches a wallet, exchange
account, or executes trades.
