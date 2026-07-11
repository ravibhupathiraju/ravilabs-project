# Swing Trade Tracker

A stock screener and backtester for swing trading, built around the **RSI(2) mean-reversion pullback strategy** (popularized by Larry Connors). This strategy historically shows a high win rate (~70-80%) on liquid, trending large-cap stocks because it buys short-term oversold dips inside long-term uptrends.

## Strategy Rules

**Entry (long):**
1. Close > 200-day SMA (only trade stocks in long-term uptrends)
2. RSI(2) < 10 (deep short-term oversold pullback)
3. Close < 5-day SMA (price is stretched below its short-term mean)
4. Liquidity filter: price > $5 and 20-day avg volume > 500k shares

**Exit:**
- Close > 5-day SMA (mean reversion complete), or
- RSI(2) > 70, or
- Hard stop: entry price - 2 x ATR(14), or
- Time stop: 10 trading days

## Install

```bash
pip install -r requirements.txt
```

## Usage

Scan the default watchlist for buy signals today:

```bash
python app.py scan
```

Scan specific tickers:

```bash
python app.py scan --tickers AAPL MSFT NVDA
```

Backtest the strategy to verify the win rate:

```bash
python app.py backtest --tickers AAPL MSFT NVDA --years 5
```

## Disclaimer

This tool is for educational purposes only and is not financial advice. Past performance does not guarantee future results. Always do your own research and manage risk.
