# MT5 port of the swing strategies

`SwingStrategies.mq5` runs all seven swing strategies from this project inside the
MetaTrader 5 Strategy Tester. One input (`InpStrategy`) picks the strategy, so
**optimizing that one input over 0..6 backtests all seven and gives you a
comparison table** — the MT5 equivalent of the app's backtest results screen.

## Install

1. In MT5: **File → Open Data Folder**, then go to `MQL5\Experts\`.
2. Copy `SwingStrategies.mq5` there.
3. In MetaEditor (F4): open it and press **F7** to compile.
4. Copy `SwingStrategies.set` into `MQL5\Presets\`.

## Run the backtest

1. **View → Strategy Tester** (Ctrl+R).
2. Expert: `SwingStrategies`. Symbol: any stock in your list. Period: **D1**.
3. Modelling: **1 minute OHLC** (or *Every tick* for more accuracy).
   Do **not** use *Open prices only* — the ATR stops and targets fill intrabar
   and would be mismodelled.
4. **Inputs tab → Load** → `SwingStrategies.set`. Edit `InpSymbolList` to your
   broker's symbol names.
5. Set **Optimization: Slow complete algorithm**, then **Start**.
   Seven passes run (one per strategy); the **Optimization Results** tab is
   your comparison table — sort by Expected Payoff or Profit Factor.

To test a single strategy, untick optimization and set `InpStrategy` directly.

## What is faithful to the Python version

- **Rules**: all seven entry/exit rule sets are ported verbatim, including the
  Bear CRWV 0–80 score (trend, pullback, bars-since-high, RSI, relative volume).
- **Indicators**: computed from raw OHLCV in the EA rather than with `iRSI`/`iMA`,
  so they match `indicators.py` exactly — Wilder RSI/ATR, an **EMA-9** MACD
  signal line (MT5's built-in MACD uses an SMA signal), and a sample-stdev
  Bollinger band (MT5's uses population stdev).
- **Execution**: signal on the closed daily bar → **fill at the next bar's open**;
  the ATR stop and ATR target are fixed at the fill and sent as SL/TP, so they
  fill intrabar at their price; one position at a time per symbol.
- **Risk parameters** per strategy (stop ×ATR / target ×ATR / max hold days) are
  identical to `strategies.py`.

## Where results will legitimately differ

These are platform limits, not bugs — expect numbers that differ from the app:

1. **No earnings filter.** MT5 has no earnings calendar, so the ±3-day earnings
   exclusion (and the pre-earnings close) is absent. The Python backtest skips
   trades this EA will take. This is the single biggest source of divergence.
2. **Signal exits fill at the next open, not the close.** The Python backtest
   exits at that bar's close; a bar-based EA can only act once the bar has
   closed, i.e. at the next open. Stops and targets are unaffected (they are
   SL/TP orders and fill intrabar at their price).
3. **Unadjusted prices.** yfinance data is split/dividend-adjusted
   (`auto_adjust=True`); broker CFD history usually is not.
4. **Volume.** Many stock-CFD feeds have no real exchange volume. The EA falls
   back to tick volume and prints a warning. If so, **set `InpMinAvgVolume=0`**
   (otherwise the liquidity filter rejects everything) and treat the 20-Day
   Breakout's 1.5× volume test and the Bear score's relative-volume points as
   approximations only.
5. **Costs.** The MT5 test applies your broker's real spread, commission and
   swap; the Python backtest applies none. MT5 will look worse — that is the
   more honest number.
6. **Data depth.** Broker stock history is often only a few years; the Python
   app can pull much longer.

## Sizing

`InpRiskPercent` (default 1%) sizes each position from the stop distance:
lots = (equity × risk%) ÷ (stop distance × tick value). Set it to 0 to use
`InpFixedLots` instead. If the computed size is below the symbol's minimum lot,
the trade is skipped.
