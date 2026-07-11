"""Earnings-date awareness.

Entries within the earnings buffer window are skipped, and open positions are
closed the day before the window starts. Uses Yahoo Finance earnings
calendars; when earnings data is unavailable for a ticker, no exclusion is
applied (a note is printed once per ticker).
"""

import pandas as pd
import yfinance as yf


def _naive(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts.normalize()


def earnings_dates(ticker: str) -> list[pd.Timestamp]:
    """Past and upcoming earnings dates for a ticker (empty when unavailable)."""
    try:
        df = yf.Ticker(ticker).get_earnings_dates(limit=60)
    except Exception:
        df = None
    if df is None or df.empty:
        print(f"  ! no earnings data for {ticker}; earnings filter skipped")
        return []
    return sorted({_naive(d) for d in df.index})


def near_earnings(date, dates: list[pd.Timestamp], buffer_days: int = 3) -> bool:
    """True when `date` falls within +/- buffer_days of any earnings date."""
    if not dates:
        return False
    d = _naive(date)
    return any(abs((d - e).days) <= buffer_days for e in dates)
