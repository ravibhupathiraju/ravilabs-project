"""Technical indicators implemented with pandas (no TA-Lib dependency)."""

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=window, min_periods=window).mean()


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
    """Average True Range for volatility-based stops.

    Expects columns: High, Low, Close.
    """
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Add all indicator columns needed by the strategy."""
    out = df.copy()
    out["sma5"] = sma(out["Close"], 5)
    out["sma200"] = sma(out["Close"], 200)
    out["rsi2"] = rsi(out["Close"], 2)
    out["atr14"] = atr(out, 14)
    out["avg_vol20"] = out["Volume"].rolling(20, min_periods=20).mean()
    return out
