"""Event-driven backtester to verify the strategy's win rate and expectancy.

Execution model: when a signal fires on day T's close, the position is opened
at day T+1's open (no look-ahead bias). Exits are evaluated on each
subsequent bar; stop-outs fill at the stop price, other exits fill at the
close.
"""

from dataclasses import dataclass, field

import pandas as pd

from .data import load_history
from .strategy import ATR_STOP_MULT, entry_conditions, exit_conditions


@dataclass
class Trade:
    ticker: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    days_held: int
    exit_reason: str

    @property
    def pnl_pct(self) -> float:
        return (self.exit_price / self.entry_price - 1.0) * 100.0


@dataclass
class Results:
    trades: list[Trade] = field(default_factory=list)

    def summary(self) -> str:
        n = len(self.trades)
        if n == 0:
            return "No trades generated."
        wins = [t for t in self.trades if t.pnl_pct > 0]
        losses = [t for t in self.trades if t.pnl_pct <= 0]
        win_rate = 100.0 * len(wins) / n
        avg_win = sum(t.pnl_pct for t in wins) / len(wins) if wins else 0.0
        avg_loss = sum(t.pnl_pct for t in losses) / len(losses) if losses else 0.0
        gross_win = sum(t.pnl_pct for t in wins)
        gross_loss = abs(sum(t.pnl_pct for t in losses))
        profit_factor = gross_win / gross_loss if gross_loss else float("inf")
        expectancy = sum(t.pnl_pct for t in self.trades) / n
        avg_days = sum(t.days_held for t in self.trades) / n
        return (
            f"Trades:        {n}\n"
            f"Win rate:      {win_rate:.1f}%\n"
            f"Avg win:       {avg_win:+.2f}%\n"
            f"Avg loss:      {avg_loss:+.2f}%\n"
            f"Expectancy:    {expectancy:+.2f}% per trade\n"
            f"Profit factor: {profit_factor:.2f}\n"
            f"Avg hold:      {avg_days:.1f} days"
        )


def backtest_ticker(ticker: str, df: pd.DataFrame) -> list[Trade]:
    trades: list[Trade] = []
    i = 0
    n = len(df)
    while i < n - 1:
        row = df.iloc[i]
        if not entry_conditions(row):
            i += 1
            continue
        # Enter at next bar's open.
        entry_idx = i + 1
        entry_price = float(df.iloc[entry_idx]["Open"])
        entry_date = str(df.index[entry_idx].date())
        stop = entry_price - ATR_STOP_MULT * float(row["atr14"])
        exit_price, exit_reason, exit_idx = None, None, None
        for j in range(entry_idx, n):
            bar = df.iloc[j]
            days_held = j - entry_idx
            reason = exit_conditions(bar, entry_price, days_held)
            if reason:
                exit_reason = reason
                exit_idx = j
                exit_price = stop if reason == "stop" else float(bar["Close"])
                break
        if exit_idx is None:  # still open at end of data; close at last bar
            exit_idx = n - 1
            exit_price = float(df.iloc[-1]["Close"])
            exit_reason = "end_of_data"
        trades.append(
            Trade(
                ticker=ticker,
                entry_date=entry_date,
                exit_date=str(df.index[exit_idx].date()),
                entry_price=entry_price,
                exit_price=exit_price,
                days_held=exit_idx - entry_idx,
                exit_reason=exit_reason,
            )
        )
        i = exit_idx + 1  # one position at a time per ticker
    return trades


def run(tickers: list[str], years: int = 5) -> Results:
    results = Results()
    for ticker in tickers:
        df = load_history(ticker, years=years)
        if df.empty:
            continue
        trades = backtest_ticker(ticker, df)
        results.trades.extend(trades)
        print(f"  {ticker}: {len(trades)} trades")
    return results
