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

## Usage

Build the strategy comparison dashboard (backtests all six strategies and writes `dashboard.html`):

```bash
python app.py dashboard --years 5
```

Open `dashboard.html` in a browser to see trades, win rate, avg win/loss, expectancy, profit factor, avg hold, max drawdown, exit-reason breakdown, and comparison charts per strategy.

Scan the watchlist for buy signals today (all strategies, earnings-filtered):

```bash
python app.py scan
python app.py scan --strategy rsi2 --tickers AAPL MSFT NVDA
```

Backtest in the terminal:

```bash
python app.py backtest --years 5
python app.py backtest --strategy breakout20 --earnings-buffer 5
```

## Backtest methodology

- Signals on day T's close are filled at day T+1's open (no look-ahead bias)
- Stops and ATR profit targets are fixed at entry and fill at their price; other exits fill at the close
- One position at a time per ticker per strategy
- Earnings dates come from Yahoo Finance; when unavailable for a ticker, the filter is skipped with a printed note

## Disclaimer

This tool is for educational purposes only and is not financial advice. Past performance does not guarantee future results. Always do your own research and manage risk.
