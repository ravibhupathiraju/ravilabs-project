"""Market data access via yfinance."""

import pandas as pd
import yfinance as yf

from .indicators import enrich


def load_history(ticker: str, years: int = 2) -> pd.DataFrame:
    """Download daily OHLCV history and attach indicator columns.

    Returns an empty DataFrame when the ticker cannot be fetched, so callers
    can skip it gracefully.
    """
    try:
        df = yf.download(
            ticker,
            period=f"{years}y",
            interval="1d",
            auto_adjust=True,
            progress=False,
        )
    except Exception as exc:  # network / bad ticker
        print(f"  ! failed to fetch {ticker}: {exc}")
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    # yfinance may return MultiIndex columns for single tickers.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Close"])
    return enrich(df)


def read_watchlist(path: str = "watchlist.txt") -> list[str]:
    """Read tickers from a plain-text watchlist, one symbol per line."""
    with open(path, encoding="utf-8") as fh:
        return [line.strip().upper() for line in fh if line.strip() and not line.startswith("#")]
