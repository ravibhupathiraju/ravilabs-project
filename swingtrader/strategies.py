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
    # True when stop/target are absolute chart levels fixed at signal time
    # (e.g. a swing low / prior pivot high) rather than ATR offsets from entry.
    absolute_levels = False

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

    def target_price(self, row: pd.Series) -> float | None:
        if self.target_atr_mult is None:
            return None
        return float(row["Close"] + self.target_atr_mult * row["atr14"])

    def planned_exit(self) -> str:
        parts = [f"{self.stop_atr_mult}xATR stop"]
        if self.target_atr_mult:
            parts.append(f"{self.target_atr_mult}xATR target")
        parts.append(f"signal exit, {self.max_hold_days}d time stop")
        return ", ".join(parts)

    def exit_trigger(self, row: pd.Series) -> tuple[str, float | None] | None:
        """Today's level of the signal-exit condition, for display.

        The level is recomputed daily, so it drifts while the trade is on;
        it answers "roughly where would I take profit as of right now?"
        """
        return None

    def initial_stop(self, sig_row: pd.Series, entry_price: float) -> float:
        """Protective stop fixed at entry. Default: ATR offset from the fill."""
        return entry_price - self.stop_atr_mult * float(sig_row["atr14"])

    def initial_target(self, sig_row: pd.Series, entry_price: float) -> float | None:
        """Profit target fixed at entry, or None for signal-exit strategies."""
        if self.target_atr_mult is None:
            return None
        return entry_price + self.target_atr_mult * float(sig_row["atr14"])


class RSI2MeanReversion(Strategy):
    name = "rsi2"
    label = "RSI(2) Mean Reversion"
    description = "Buy deep RSI(2) pullbacks in long-term uptrends; exit on snap-back above SMA5."
    required = ("sma5", "sma200", "rsi2")

    def setup(self, row):
        return row["Close"] > row["sma200"] and row["rsi2"] < 10 and row["Close"] < row["sma5"]

    def signal_exit(self, row):
        return row["Close"] > row["sma5"] or row["rsi2"] > 70

    def exit_trigger(self, row):
        return ("close above SMA5", float(row["sma5"]))


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

    def exit_trigger(self, row):
        return ("close at 7-day high", float(row["hi7"]))


class BollingerSnapback(Strategy):
    name = "bollinger"
    label = "Bollinger Snapback"
    description = "Buy closes below the lower Bollinger Band in uptrends; exit at the middle band."
    required = ("sma20", "sma200", "bb_lower")

    def setup(self, row):
        return row["Close"] > row["sma200"] and row["Close"] < row["bb_lower"]

    def signal_exit(self, row):
        return row["Close"] > row["sma20"]

    def exit_trigger(self, row):
        return ("close above middle band (SMA20)", float(row["sma20"]))


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

    def exit_trigger(self, row):
        return ("close below 10-day low (trails up)", float(row["lo10_prior"]))


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

    def exit_trigger(self, row):
        return ("MACD bearish cross (no price level)", None)


class BearCRWV(Strategy):
    """TradeLab "CRWV" scored pullback model, adapted to a binary signal so
    it plugs into the screener, backtester and alert monitor.

    The full model ranks tickers by a 0-100 score (see bear.py). For alerts
    a threshold is needed: the per-ticker score (max 80, market regime
    excluded) must reach BEAR_MIN_SCORE. 45 corresponds to a TradeLab
    "Medium/High" pick on a bull-market day (45 + 20 regime = 65).

    Levels match TradeLab: stop 1.5xATR, target 3xATR, 10-day hold.
    """

    BEAR_MIN_SCORE = 45

    name = "bear"
    label = "Bear CRWV Pullback Score"
    description = (
        "TradeLab CRWV model: uptrend (EMA5>EMA21>EMA50, EMA21 rising), healthy "
        "10-30% pullback from the 60-day high, RSI(14)<45 and volume expansion, "
        "scored 0-100; alert when the per-ticker score reaches 45. Stop 1.5xATR, "
        "target 3xATR, 10-day hold."
    )
    required = ("bear_score", "pullback_pct", "bars_since_high", "rel_vol20", "rsi14")
    stop_atr_mult = 1.5
    target_atr_mult = 3.0
    max_hold_days = 10

    def setup(self, row):
        return int(row["bear_score"]) >= self.BEAR_MIN_SCORE

    def exit_trigger(self, row):
        return ("3xATR target (fixed at entry)", None)


ALL = [
    RSI2MeanReversion(),
    Double7s(),
    BollingerSnapback(),
    TrendPullback(),
    DonchianBreakout(),
    MACDTrend(),
    BearCRWV(),
]
STRATEGIES = {s.name: s for s in ALL}
