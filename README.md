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

## Alerts / Trades tab (consolidated) + live monitors

Monitors are started from their own tabs — **0DTE intraday** (Start 0DTE monitor, using the backtest's symbol/strategy checkboxes), **Swing strategies** (Start swing alerts, using the checked strategies only), **Bear strategy**, and **Analyzer** (reversal watch). All of them log to one consolidated **Alerts / Trades** tab: filter by date range / type (0DTE, swing, reversal) / ticker, with a **P/L summary** per ticker (closed trades, wins, total and average P/L %, exact paper $ P/L where an Alpaca order was placed) plus a TOTAL row, and the full alert history beneath. A **Today** button snaps the filters to a single-day view.

- The monitor is a background poller (every 60s during 9:30–16:00 ET, Mon–Fri) that evaluates the selected strategies on today's data for the selected symbols
- On an entry signal a **Windows desktop notification** fires with the entry time, entry price, stop price, and planned exit; a second notification fires when the trade exits (stop hit, condition met, or 16:00 close)
- No one-alert-per-day limit: after an ORB stop-out or a VWAP-touch exit, the next signal the same session fires a fresh, numbered alert (Momentum-30, Gap Fade and the straddle have a single decision point per day by nature)
- **Swing alerts** scan a chosen ticker universe (watchlist, top 50, S&P 100) every 15 minutes — plus once at monitor start, so an evening start still catches the day's close signals. Lifecycle mirrors the backtest: a **signal** alert on the setup (close, provisional stop, planned exit) → an **entry** alert when the position fills at the next session's open (exact ATR stop and target) → an **exit** alert on stop, profit target, signal exit, time stop, or pre-earnings close, with P/L. Tracking reconstructs correctly even if the app was off for a few days
- Every alert is recorded to a local SQLite database (`alerts.db`) and shown in the **Alert history** table, filterable by date range, symbol, and type (0DTE/swing), with alert time, entry price, exit price, and P/L

Keep `python webapp.py` running for alerts to fire (the browser tab may be closed). yfinance data lags real time by a minute or two — confirm prices on your own quotes before trading.

### Paper trading (Alpaca)

Swing alerts can be mirrored to a free [Alpaca](https://alpaca.markets) **paper** account so signals become real simulated orders with broker-side fills:

1. Sign up at alpaca.markets (free, no funding), switch the dashboard to **Paper** mode, and generate API keys
2. Copy `alpaca.example.json` to `alpaca.json` (git-ignored) and paste the keys; `notional_per_trade` sets the $ sizing per signal (whole shares). The standard `APCA_API_KEY_ID`/`APCA_API_SECRET_KEY` environment variables work too
3. Restart the app — the **Paper trading (Alpaca)** panel in the alerts tab shows account equity, open positions, and pending orders

The panel appears on both the **Swing strategies** and **0DTE alerts** tabs (same account, different rules per kind).

**Swing orders**: a signal submits a GTC **bracket** market buy — after the close it queues and fills at the next open (the backtest's execution model), with the ATR stop and any profit target resting server-side at Alpaca, so they trigger even while this app is offline. An exit alert (signal exit, time stop, pre-earnings) closes the position; stop/target exits usually fill broker-side first, making the close a no-op. Orders are only submitted on **completed daily bars** — intraday signals are provisional (partial bar), so a 16:15 ET rescan confirms which survived to the close before anything is sent.

**0DTE orders**: alerting and paper trading are deliberately decoupled. **All five strategies alert and log to Alert history; only the strategies in `zerodte_strategies` are actually traded** (default: `["vwap"]`). 0DTE orders are plain **day market** orders, long or short, sized from `zerodte_notional` — not brackets, because the VWAP exit is a moving level, not a fixed price. The monitor closes the position when its exit alert fires (VWAP touched), and a **15:55 ET sweep flattens any 0DTE position still open**, so nothing intraday carries overnight (the sweep is scoped to the 0DTE symbols and never touches swing positions). **SPX is alert-only** — it is a cash index, not tradable as an equity; SPY, QQQ and IWM trade normally, including short.

**Guardrails** (checked against the live account before every order): at most `max_positions` concurrent positions (default 10), total exposure ≤ `max_exposure_pct` of equity (default 100 = no leverage), and one position per symbol. A full-universe scan can produce 40+ simultaneous signals; without these caps they would blow past the account's overnight buying power and be rejected in a race. Signals beyond a cap are skipped and logged, never queued. Share counts are recorded per alert and shown in the **Paper** column of Alert history.

The module only ever talks to the paper endpoint — it cannot place live trades.

## Data sources (hybrid)

- **yfinance** — daily OHLCV history for backtests, scans and swing tracking (long history, split/dividend-adjusted), plus intraday fallback
- **Alpaca Market Data (free IEX feed)** — live 5-minute bars and latest quotes for the 0DTE monitor and the Analyzer, using the same `alpaca.json` credentials as paper trading. Real-time prices (yfinance lags 1–2 min), and analysis prices come from the same venue paper orders fill on

Every Alpaca call falls back to yfinance automatically (no keys, cash indices like SPX/^GSPC, request failure), so the app runs unchanged without credentials. Note the free IEX feed reports IEX-exchange volume only (~3% of consolidated tape): prices are reliable, but volume-vs-average comparisons always use consolidated daily-bar volume — the code handles this. Earnings calendar still comes from Yahoo, which has been flaky; a Finnhub integration is the planned fix.

## Bear strategy tab (TradeLab CRWV pullback score)

A dedicated tab hosting a faithful port of the TradeLab "CRWV" swing model — a 0–100 scored pullback-in-uptrend system (validated to produce identical scores, levels and reasons on TradeLab's own research output):

- **Trend (25)**: EMA5 > EMA21 > EMA50 (+10, counted twice as upstream), EMA21 rising over 10 bars (+5)
- **Pullback (25)**: 10–30% below the 60-day high (+15); that high printed 5–25 bars ago (+10)
- **Momentum (10)**: RSI(14) < 45 · **Volume (10)**: > 1.2× 20-day average volume
- **Market regime (20)**: SPY > EMA200 (+10), SPY > EMA50 (+5), EMA50 > EMA200 (+5)
- **Levels**: entry = scan close; stop = close − 1.5×ATR; target = close + 3×ATR; confidence ≥85 Very High / ≥70 High / ≥55 Medium

The tab closes the full research loop: **Scan today** ranks the top 20 candidates with a market-regime banner; **Run research** replicates TradeLab's methodology (weekly scans → top 20 → 10-trading-day hold → per-date return / win% / target% / stop% and a summary average); **alerts** run the model live (per-ticker score ≥ 45 fires a signal → fill at next open → 1.5×ATR stop / 3×ATR target / 10-day time stop, with desktop notifications); and the **live performance panel** (win rate, expectancy, profit factor, cumulative P/L curve over logged alerts) shows what the model actually delivered vs. the research. The TradeLab 151-symbol universe ships as `tradable.txt` and is selectable everywhere; the strategy also appears in the regular swing backtest/scan/alerts as "Bear CRWV Pullback Score".

## Backtest methodology

- Signals on day T's close are filled at day T+1's open (no look-ahead bias)
- Stops and ATR profit targets are fixed at entry and fill at their price; other exits fill at the close
- One position at a time per ticker per strategy
- Earnings dates come from Yahoo Finance; when unavailable for a ticker, the filter is skipped with a printed note

## Disclaimer

This tool is for educational purposes only and is not financial advice. Past performance does not guarantee future results. Always do your own research and manage risk.
