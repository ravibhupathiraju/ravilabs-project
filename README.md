# Swing Trade Tracker

A multi-strategy stock screener, backtester, and comparison dashboard for swing trading. Six classic swing strategies run behind a common interface so their results are directly comparable, and all trading around earnings dates is excluded.

## Strategies

| Key | Strategy | Style | Core idea |
|---|---|---|---|
| `rsi2` | RSI(2) Mean Reversion | Mean reversion | Buy deep RSI(2) < 10 pullbacks above the 200-day SMA; exit on close > SMA5 |
| `double7` | Double 7s | Mean reversion | Buy 7-day closing lows above the 200-day SMA; exit at 7-day closing highs |
| `bollinger` | Bollinger Snapback | Mean reversion | Buy closes below the lower band in uptrends; exit at the middle band |
| `pullback20` | SMA20 Trend Pullback | Trend pullback | Buy tags of the 20-day SMA in strong trends; 3x ATR target, 1.5x ATR stop |
| `breakout20` | 20-Day Breakout | Momentum | Buy new 20-day highs on 1.5x volume; exit below the 10-day low |
| `macd` | MACD Trend Cross | Trend following | Buy MACD bullish crosses above the 200-day SMA; exit on bearish cross |

Every strategy shares:
- **Liquidity filter**: price > $5, 20-day average volume > 500k shares
- **ATR hard stop** fixed at entry, plus a **time stop**
- **Earnings exclusion**: no entries within +/- N days of an earnings date (default 3), and open positions are closed before an earnings window starts

## Install

```bash
pip install -r requirements.txt
```

## Web app (primary interface)

```bash
python webapp.py
```

Then open http://127.0.0.1:5000 in a browser. From the page you can:

- **Pick a duration**: 1 month, 2 months, 3 months, 6 months, 1, 2, 3, or 5 years
- **Pick a ticker universe**: My watchlist (20), Top 50 large caps (50), S&P 100 (~100), or a custom list you paste in; optionally cap the count with Max tickers
- **Select strategies** and the earnings buffer
- **Run backtest**: strategy-wise results table, win rate and expectancy charts, exit-reason breakdown
- **Scan today**: current buy signals across the selected universe and strategies

Short durations always work correctly: extra warmup history is loaded for the 200-day SMA, but trades are only opened inside the selected window. Note that larger universes take longer since price and earnings data is fetched per ticker (downloads run in parallel).

## CLI (optional)

```bash
python app.py scan
python app.py backtest --duration 6m --strategy rsi2
python app.py dashboard --duration 5y   # writes static dashboard.html
```

## 0DTE intraday tab

A second tab backtests five same-day (open and close the same session) strategies on **SPY, SPX (^GSPC), QQQ, IWM** over any calendar date range you pick:

| Strategy | Idea | Data |
|---|---|---|
| ORB 15-min Breakout | Trade the break of the first 15 minutes, stop at the other side of the range, exit by the close (Zarattini & Aziz, 2023) | 5-min bars |
| First 30-min Momentum | The first half hour's direction persists into the close (Gao, Han, Li & Zhou, 2018); enter 10:00, exit at close | 5-min bars |
| VWAP Reversion | Fade 0.3%+ stretches from session VWAP back to VWAP | 5-min bars |
| Gap Fade | Fade overnight gaps > 0.3% toward the prior close; exit at close | Daily bars |
| Expected-Move Straddle Sell (proxy) | The classic 0DTE premium sell: short an ATM straddle priced at 0.8x the 20-day 1-sigma move; PnL = premium - abs(open-to-close move) | Daily bars |

**Limitations to know:** free 5-minute data (yfinance) only covers ~60 days, so the three intraday strategies clip older date ranges (the UI warns when this happens). Gap Fade and the straddle proxy use daily bars and work for any range. The straddle sell is a volatility proxy in % of the underlying, not real option prices; true 0DTE options backtests require paid historical options data (for example CBOE DataShop).

## Day trade scanner (separate page)

A standalone intraday relative-strength scanner and backtester at **http://127.0.0.1:5000/daytrade** (linked from the main page; it does not touch the Swing or 0DTE tabs). It replays the scan bar by bar on 5-minute data for a built-in list of ~300 liquid, optionable names grouped by theme (`swingtrader/daytrade/universe.py`).

- **iRS**: % distance of price from the middle of today's high-low range so far, ranked 0-99 across the universe every 5 minutes. **5D** (live) and **20D** (as of the prior close) are return ranks.
- **Signal**: the first time a ticker reaches iRS >= 95 (call) or <= 5 (put) that day. "Wide bands" also records 90-94 and 6-10.
- **Outcome** (bars after the signal only): best and worst move in the trade's direction, close, and whether it reached +2%. A simple exit simulation takes profit or stops out (stop assumed first when a bar touches both), else exits at the close. All in % of the stock, not option prices.
- **Filters**, each scored against the baseline: first seen by 11:00, day % agrees, 5D agrees, sector cluster (3+ same theme within 60 min), minority side of a lopsided call/put count, gap-down reversal calls, gap-up fade puts, puts off the low, fallen-leader puts, and combinations. Thresholds are constants in `swingtrader/daytrade/filters.py`.

```bash
python -m swingtrader.daytrade                      # full universe, prints tables
python -m swingtrader.daytrade --wide-bands --csv signals.csv
python -m swingtrader.daytrade --cached --target 1.5 --stop 0.75
```

Bars are cached in `.cache/daytrade/` (override with `DAYTRADE_CACHE`). Each download merges into the cache, so although yfinance only serves ~60 days of 5-minute bars, history keeps growing if you run it at least every ~50 days. The first full download takes several minutes.

## Backtest methodology

- Signals on day T's close are filled at day T+1's open (no look-ahead bias)
- Stops and ATR profit targets are fixed at entry and fill at their price; other exits fill at the close
- One position at a time per ticker per strategy
- Earnings dates come from Yahoo Finance; when unavailable for a ticker, the filter is skipped with a printed note

## Disclaimer

This tool is for educational purposes only and is not financial advice. Past performance does not guarantee future results. Always do your own research and manage risk.
