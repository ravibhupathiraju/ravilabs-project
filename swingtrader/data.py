"""Market data access via yfinance."""

import pandas as pd
import yfinance as yf

from .indicators import enrich


def parse_duration(text: str) -> int:
    """Convert a duration like '1m', '6m', or '2y' to calendar days."""
    text = str(text).strip().lower()
    try:
        n, unit = int(text[:-1]), text[-1]
    except (ValueError, IndexError):
        raise ValueError(f"invalid duration {text!r}; use e.g. '1m', '6m', '2y'") from None
    if n <= 0 or unit not in ("m", "y"):
        raise ValueError(f"invalid duration {text!r}; use e.g. '1m', '6m', '2y'")
    return n * 30 if unit == "m" else n * 365


OHLCV = ["Open", "High", "Low", "Close", "Volume"]


def load_history(ticker: str, days: int = 730) -> pd.DataFrame:
    """Download daily OHLCV history going `days` back and attach indicators.

    Uses yf.Ticker().history(), which always returns flat single-level
    columns and is safe to call from worker threads (yf.download can return
    duplicated/MultiIndex columns under concurrency).

    Returns an empty DataFrame when the ticker cannot be fetched, so callers
    can skip it gracefully.
    """
    start = (pd.Timestamp.today() - pd.Timedelta(days=days)).date()
    try:
        df = yf.Ticker(ticker).history(
            start=str(start),
            interval="1d",
            auto_adjust=True,
        )
    except Exception as exc:  # network / bad ticker
        print(f"  ! failed to fetch {ticker}: {exc}")
        return pd.DataFrame()
    if df is None or df.empty:
        print(f"  ! no price data for {ticker}; skipped")
        return pd.DataFrame()
    # Defensive normalization: flatten any MultiIndex and drop duplicates.
    if isinstance(df.columns, pd.MultiIndex):
        level = 0 if "Close" in df.columns.get_level_values(0) else -1
        df.columns = df.columns.get_level_values(level)
    df = df.loc[:, ~df.columns.duplicated()]
    missing = [c for c in OHLCV if c not in df.columns]
    if missing:
        print(f"  ! {ticker} missing columns {missing}; skipped")
        return pd.DataFrame()
    df = df[OHLCV]
    # Normalize to tz-naive index so date comparisons are safe.
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    df = df.dropna(subset=["Close"])
    return enrich(df)


def read_watchlist(path: str = "watchlist.txt") -> list[str]:
    """Read tickers from a plain-text watchlist, one symbol per line."""
    with open(path, encoding="utf-8") as fh:
        return [line.strip().upper() for line in fh if line.strip() and not line.startswith("#")]
