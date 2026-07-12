"""Technical indicators implemented with pandas (no TA-Lib dependency)."""

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def rsi(series: pd.Series, period: int = 2) -> pd.Series:
    """Wilder's RSI. Short periods (2-4) are used for mean-reversion entries."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # When avg_loss is 0 (all gains), RSI is 100.
    return out.fillna(100.0).where(avg_gain.notna(), np.nan)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range for volatility-based stops."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Add every indicator column used by any of the strategies."""
    out = df.copy()
    close = out["Close"]

    # Moving averages
    out["sma5"] = sma(close, 5)
    out["sma20"] = sma(close, 20)
    out["sma50"] = sma(close, 50)
    out["sma200"] = sma(close, 200)

    # Oscillators
    out["rsi2"] = rsi(close, 2)
    out["rsi14"] = rsi(close, 14)

    # Volatility and volume
    out["atr14"] = atr(out, 14)
    out["avg_vol20"] = out["Volume"].rolling(20, min_periods=20).mean()

    # Bollinger Bands (20, 2)
    std20 = close.rolling(20, min_periods=20).std()
    out["bb_upper"] = out["sma20"] + 2.0 * std20
    out["bb_lower"] = out["sma20"] - 2.0 * std20

    # MACD (12, 26, 9) with cross flags
    macd_line = ema(close, 12) - ema(close, 26)
    signal = macd_line.ewm(span=9, adjust=False).mean()
    out["macd"] = macd_line
    out["macd_signal"] = signal
    out["macd_cross_up"] = (macd_line > signal) & (macd_line.shift(1) <= signal.shift(1))
    out["macd_cross_down"] = (macd_line < signal) & (macd_line.shift(1) >= signal.shift(1))

    # Channels / rolling extremes
    out["hi20_prior"] = out["High"].rolling(20, min_periods=20).max().shift(1)
    out["lo10_prior"] = out["Low"].rolling(10, min_periods=10).min().shift(1)
    out["lo7"] = close.rolling(7, min_periods=7).min()
    out["hi7"] = close.rolling(7, min_periods=7).max()

    _bear_columns(out)
    return out


# --- Bear (TradeLab CRWV) score columns -----------------------------------
# Faithful port of the TradeLab CRWV swing strategy: a 0-100 scored
# pullback-in-uptrend model. The per-ticker part (max 80 points) is computed
# here as columns; the SPY market-regime part (max 20 points) is added at
# scan time in bear.py since it is the same for every ticker on a given day.
#
# Component points (TradeLab crwv_strategy.py):
#   +10 TREND_UP        EMA5 > EMA21 > EMA50
#   +10 EMA_ALIGNMENT   same condition (scored twice upstream; kept faithful)
#   +5  EMA21 rising    EMA21 today > EMA21 ten bars ago
#   +15 healthy pullback  10% <= pullback from 60-day high <= 30%
#   +10 bars since high   5 <= bars since that 60-day high <= 25
#   +10 RSI(14) < 45
#   +10 relative volume > 1.20x the 20-day average

BEAR_SCORE_MAX = 80  # per-ticker points (market regime adds up to 20)


def _bear_columns(out: pd.DataFrame) -> None:
    close = out["Close"]
    out["ema5"] = close.ewm(span=5, adjust=False).mean()
    out["ema21"] = close.ewm(span=21, adjust=False).mean()
    out["ema50"] = close.ewm(span=50, adjust=False).mean()
    out["rel_vol20"] = out["Volume"] / out["avg_vol20"]

    # Pullback from the 60-day high, and how many bars ago that high printed.
    hi60 = out["High"].rolling(60, min_periods=60).max()
    out["pullback_pct"] = (hi60 - close) / hi60 * 100.0
    high = out["High"].to_numpy(dtype=float)
    n = len(out)
    bars_since = np.full(n, np.nan)
    for t in range(59, n):
        window = high[t - 59 : t + 1]
        if not np.isnan(window).any():
            bars_since[t] = 59 - int(window.argmax())
    out["bars_since_high"] = bars_since

    trend_up = (out["ema5"] > out["ema21"]) & (out["ema21"] > out["ema50"])
    out["trend_up"] = trend_up
    out["ema21_rising"] = out["ema21"] > out["ema21"].shift(10)

    out["bear_score"] = (
        10 * trend_up.astype(int)
        + 10 * trend_up.astype(int)  # EMA_ALIGNMENT duplicates TREND_UP upstream
        + 5 * out["ema21_rising"].astype(int)
        + 15 * out["pullback_pct"].between(10, 30).astype(int)
        + 10 * out["bars_since_high"].between(5, 25).astype(int)
        + 10 * (out["rsi14"] < 45).astype(int)
        + 10 * (out["rel_vol20"] > 1.20).astype(int)
    )
