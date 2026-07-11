"""RSI(2) mean-reversion pullback strategy (Connors-style).

Why this strategy: buying 2-period-RSI oversold dips in stocks trading above
their 200-day SMA is one of the most robust, extensively tested high win-rate
swing setups. Most trades resolve in 2-6 days as price snaps back to its
short-term mean, which produces a high percentage of small winners; the trend
filter and ATR stop control the downside.
"""

from dataclasses import dataclass

import pandas as pd

# Entry thresholds
RSI_ENTRY = 10.0
MIN_PRICE = 5.0
MIN_AVG_VOLUME = 500_000

# Exit thresholds
RSI_EXIT = 70.0
ATR_STOP_MULT = 2.0
MAX_HOLD_DAYS = 10


@dataclass
class Signal:
    ticker: str
    date: str
    close: float
    rsi2: float
    sma5: float
    sma200: float
    stop_price: float
    target_note: str


def entry_conditions(row: pd.Series) -> bool:
    """True when all long-entry rules are met for a single bar."""
    required = ("Close", "sma5", "sma200", "rsi2", "atr14", "avg_vol20")
    if any(pd.isna(row.get(col)) for col in required):
        return False
    return (
        row["Close"] > row["sma200"]          # long-term uptrend
        and row["rsi2"] < RSI_ENTRY           # deep short-term oversold
        and row["Close"] < row["sma5"]        # stretched below short-term mean
        and row["Close"] > MIN_PRICE          # liquidity/quality filters
        and row["avg_vol20"] > MIN_AVG_VOLUME
    )


def exit_conditions(row: pd.Series, entry_price: float, days_held: int) -> str | None:
    """Return the exit reason for an open position, or None to keep holding."""
    stop = entry_price - ATR_STOP_MULT * row["atr14"]
    if row["Low"] <= stop:
        return "stop"
    if row["Close"] > row["sma5"]:
        return "mean_reversion"
    if row["rsi2"] > RSI_EXIT:
        return "rsi_overbought"
    if days_held >= MAX_HOLD_DAYS:
        return "time_stop"
    return None


def latest_signal(ticker: str, df: pd.DataFrame) -> Signal | None:
    """Check whether the most recent bar triggers a buy signal."""
    if df.empty:
        return None
    row = df.iloc[-1]
    if not entry_conditions(row):
        return None
    stop = float(row["Close"] - ATR_STOP_MULT * row["atr14"])
    return Signal(
        ticker=ticker,
        date=str(df.index[-1].date()),
        close=float(row["Close"]),
        rsi2=float(row["rsi2"]),
        sma5=float(row["sma5"]),
        sma200=float(row["sma200"]),
        stop_price=stop,
        target_note="Exit on close > SMA5 or RSI(2) > 70; time stop 10 days",
    )
