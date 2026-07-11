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
    return out
