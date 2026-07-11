"""Event-driven backtester comparing strategies on equal terms.

Execution model: a signal on day T's close opens a position at day T+1's open
(no look-ahead bias). Stops and ATR profit targets are fixed at entry and
fill at their price; other exits fill at the close. Entries inside the
earnings buffer window are skipped, and open positions are closed at the
close of the bar before the window starts (reason: pre_earnings).

Durations: history is always loaded with extra warmup days so the 200-day
SMA is available, but trades are only opened inside the selected window.
That makes short windows (1-2 months) work correctly.
"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import pandas as pd

from .data import load_history
from .earnings import earnings_dates, near_earnings
from .strategies import Strategy

WARMUP_DAYS = 400  # calendar days of extra history for indicator warmup


@dataclass
class Trade:
    strategy: str
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
    strategy: str
    trades: list[Trade] = field(default_factory=list)

    def stats(self) -> dict:
        n = len(self.trades)
        if n == 0:
            return {"trades": 0}
        wins = [t for t in self.trades if t.pnl_pct > 0]
        losses = [t for t in self.trades if t.pnl_pct <= 0]
        gross_win = sum(t.pnl_pct for t in wins)
        gross_loss = abs(sum(t.pnl_pct for t in losses))
        # Max drawdown of the cumulative per-trade PnL curve (in % points).
        cum = peak = max_dd = 0.0
        for t in sorted(self.trades, key=lambda t: t.exit_date):
            cum += t.pnl_pct
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)
        return {
            "trades": n,
            "win_rate": 100.0 * len(wins) / n,
            "avg_win": gross_win / len(wins) if wins else 0.0,
            "avg_loss": -gross_loss / len(losses) if losses else 0.0,
            "expectancy": sum(t.pnl_pct for t in self.trades) / n,
            "profit_factor": gross_win / gross_loss if gross_loss else float("inf"),
            "avg_hold": sum(t.days_held for t in self.trades) / n,
            "max_drawdown": max_dd,
            "exit_reasons": Counter(t.exit_reason for t in self.trades),
        }

    def summary(self) -> str:
        s = self.stats()
        if s["trades"] == 0:
            return "No trades generated."
        reasons = ", ".join(f"{k}: {v}" for k, v in s["exit_reasons"].most_common())
        return (
            f"Trades:        {s['trades']}\n"
            f"Win rate:      {s['win_rate']:.1f}%\n"
            f"Avg win:       {s['avg_win']:+.2f}%\n"
            f"Avg loss:      {s['avg_loss']:+.2f}%\n"
            f"Expectancy:    {s['expectancy']:+.2f}% per trade\n"
            f"Profit factor: {s['profit_factor']:.2f}\n"
            f"Avg hold:      {s['avg_hold']:.1f} days\n"
            f"Max drawdown:  {s['max_drawdown']:.1f}% (cumulative PnL)\n"
            f"Exits:         {reasons}"
        )


def backtest_ticker(
    strategy: Strategy,
    ticker: str,
    df: pd.DataFrame,
    edates: list,
    earnings_buffer: int = 3,
    trade_start: pd.Timestamp | None = None,
) -> list[Trade]:
    trades: list[Trade] = []
    i = 0
    n = len(df)
    while i < n - 1:
        row = df.iloc[i]
        if not strategy.entry(row):
            i += 1
            continue
        entry_idx = i + 1
        # Only take trades inside the selected window (warmup bars excluded).
        if trade_start is not None and df.index[entry_idx] < trade_start:
            i += 1
            continue
        # Earnings exclusion: never open a trade inside the buffer window.
        if near_earnings(df.index[i], edates, earnings_buffer) or near_earnings(
            df.index[entry_idx], edates, earnings_buffer
        ):
            i += 1
            continue
        entry_price = float(df.iloc[entry_idx]["Open"])
        entry_atr = float(row["atr14"])
        stop = entry_price - strategy.stop_atr_mult * entry_atr
        target = (
            entry_price + strategy.target_atr_mult * entry_atr
            if strategy.target_atr_mult
            else None
        )
        exit_idx, reason, exit_price = None, None, None
        for j in range(entry_idx, n):
            bar = df.iloc[j]
            r = strategy.exit(bar, stop, target, j - entry_idx)
            # Close before an upcoming earnings window while still holding.
            if r is None and j + 1 < n and near_earnings(df.index[j + 1], edates, earnings_buffer):
                r = "pre_earnings"
            if r:
                exit_idx, reason = j, r
                if r == "stop":
                    exit_price = stop
                elif r == "profit_target":
                    exit_price = target
                else:
                    exit_price = float(bar["Close"])
                break
        if exit_idx is None:  # still open at end of data
            exit_idx, reason = n - 1, "end_of_data"
            exit_price = float(df.iloc[-1]["Close"])
        trades.append(
            Trade(
                strategy=strategy.label,
                ticker=ticker,
                entry_date=str(df.index[entry_idx].date()),
                exit_date=str(df.index[exit_idx].date()),
                entry_price=entry_price,
                exit_price=exit_price,
                days_held=exit_idx - entry_idx,
                exit_reason=reason,
            )
        )
        i = exit_idx + 1  # one position at a time per ticker per strategy
    return trades


def _load(ticker: str, days: int):
    df = load_history(ticker, days=days)
    edates = earnings_dates(ticker) if not df.empty else []
    return ticker, df, edates


def run_all(
    tickers: list[str],
    strategies: list[Strategy],
    duration_days: int = 365,
    earnings_buffer: int = 3,
    max_workers: int = 8,
) -> dict[str, Results]:
    """Backtest every strategy over every ticker; data fetched in parallel."""
    results = {s.name: Results(strategy=s.label) for s in strategies}
    trade_start = pd.Timestamp.today().normalize() - pd.Timedelta(days=duration_days)
    total_days = duration_days + WARMUP_DAYS
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for ticker, df, edates in pool.map(lambda t: _load(t, total_days), tickers):
            if df.empty:
                continue
            for s in strategies:
                trades = backtest_ticker(
                    s, ticker, df, edates, earnings_buffer, trade_start=trade_start
                )
                results[s.name].trades.extend(trades)
            print(f"  {ticker}: done")
    return results
