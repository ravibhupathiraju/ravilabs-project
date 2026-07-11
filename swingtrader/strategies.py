"""Six swing-trading strategies behind a common interface.

Each strategy defines a setup (entry rules), an optional signal exit, and
risk parameters (ATR stop multiple, optional ATR profit target, time stop).
All strategies share the same liquidity filter so results are comparable.
"""

import pandas as pd

MIN_PRICE = 5.0
MIN_AVG_VOLUME = 500_000


def _valid(row: pd.Series, cols) -> bool:
    return not any(pd.isna(row.get(c)) for c in cols)


def _liquid(row: pd.Series) -> bool:
    return row["Close"] > MIN_PRICE and row["avg_vol20"] > MIN_AVG_VOLUME


class Strategy:
    name = "base"
    label = "Base"
    description = ""
    required: tuple = ()
    stop_atr_mult = 2.0
    target_atr_mult: float | None = None
    max_hold_days = 10

    def setup(self, row: pd.Series) -> bool:
        raise NotImplementedError

    def signal_exit(self, row: pd.Series) -> bool:
        return False

    def entry(self, row: pd.Series) -> bool:
        base = ("Close", "Low", "atr14", "avg_vol20")
        if not _valid(row, base + tuple(self.required)):
            return False
        return _liquid(row) and self.setup(row)

    def exit(self, row: pd.Series, stop: float, target: float | None, days_held: int) -> str | None:
        if row["Low"] <= stop:
            return "stop"
        if target is not None and row["High"] >= target:
            return "profit_target"
        if self.signal_exit(row):
            return "signal"
        if days_held >= self.max_hold_days:
            return "time_stop"
        return None

    def stop_price(self, row: pd.Series) -> float:
        return float(row["Close"] - self.stop_atr_mult * row["atr14"])


class RSI2MeanReversion(Strategy):
    name = "rsi2"
    label = "RSI(2) Mean Reversion"
    description = "Buy deep RSI(2) pullbacks in long-term uptrends; exit on snap-back above SMA5."
    required = ("sma5", "sma200", "rsi2")

    def setup(self, row):
        return row["Close"] > row["sma200"] and row["rsi2"] < 10 and row["Close"] < row["sma5"]

    def signal_exit(self, row):
        return row["Close"] > row["sma5"] or row["rsi2"] > 70


class Double7s(Strategy):
    name = "double7"
    label = "Double 7s"
    description = "Buy 7-day closing lows above the 200-day SMA; exit at a 7-day closing high."
    required = ("sma200", "lo7", "hi7")
    max_hold_days = 15

    def setup(self, row):
        return row["Close"] > row["sma200"] and row["Close"] <= row["lo7"]

    def signal_exit(self, row):
        return row["Close"] >= row["hi7"]


class BollingerSnapback(Strategy):
    name = "bollinger"
    label = "Bollinger Snapback"
    description = "Buy closes below the lower Bollinger Band in uptrends; exit at the middle band."
    required = ("sma20", "sma200", "bb_lower")

    def setup(self, row):
        return row["Close"] > row["sma200"] and row["Close"] < row["bb_lower"]

    def signal_exit(self, row):
        return row["Close"] > row["sma20"]


class TrendPullback(Strategy):
    name = "pullback20"
    label = "SMA20 Trend Pullback"
    description = "Buy tags of the 20-day SMA in strong trends; 3x ATR profit target, 1.5x ATR stop."
    required = ("sma20", "sma50", "sma200")
    stop_atr_mult = 1.5
    target_atr_mult = 3.0
    max_hold_days = 15

    def setup(self, row):
        return (
            row["sma50"] > row["sma200"]
            and row["Close"] > row["sma50"]
            and row["Low"] <= row["sma20"]
            and row["Close"] > row["sma20"]
        )

    def signal_exit(self, row):
        return row["Close"] < row["sma50"]


class DonchianBreakout(Strategy):
    name = "breakout20"
    label = "20-Day Breakout"
    description = "Buy new 20-day highs on above-average volume; exit below the 10-day low."
    required = ("hi20_prior", "lo10_prior", "sma200")
    stop_atr_mult = 2.5
    max_hold_days = 20

    def setup(self, row):
        return (
            row["Close"] > row["hi20_prior"]
            and row["Close"] > row["sma200"]
            and row["Volume"] > 1.5 * row["avg_vol20"]
        )

    def signal_exit(self, row):
        return row["Close"] < row["lo10_prior"]


class MACDTrend(Strategy):
    name = "macd"
    label = "MACD Trend Cross"
    description = "Buy MACD bullish crosses above the 200-day SMA; exit on the bearish cross."
    required = ("macd", "macd_signal", "sma200")
    stop_atr_mult = 2.5
    max_hold_days = 30

    def setup(self, row):
        return bool(row["macd_cross_up"]) and row["Close"] > row["sma200"]

    def signal_exit(self, row):
        return bool(row["macd_cross_down"])


ALL = [
    RSI2MeanReversion(),
    Double7s(),
    BollingerSnapback(),
    TrendPullback(),
    DonchianBreakout(),
    MACDTrend(),
]
STRATEGIES = {s.name: s for s in ALL}
