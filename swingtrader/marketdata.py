"""Hybrid market-data layer.

Division of labour:
  - yfinance      -> daily history for backtests/scans (long, split-adjusted)
  - Alpaca (IEX)  -> live intraday 5-minute bars and latest quotes, using the
                     same alpaca.json credentials as paper trading

Why: yfinance intraday lags 1-2 minutes and only serves ~60 days of 5-minute
bars; Alpaca's free IEX feed is real-time and, as a bonus, analysis prices
come from the same venue the paper orders fill on. Every function here falls
back to yfinance automatically when Alpaca credentials are missing, the
symbol is unsupported (cash indices like ^GSPC), or the request fails -- so
the app works unchanged without keys.

Caveat worth knowing: the free IEX feed reports IEX-exchange volume only
(a few % of consolidated tape). Prices are real-time and reliable; absolute
volume is NOT comparable with yfinance's consolidated volume. VWAP built
from IEX volume weights is a good approximation; "volume vs 20-day average"
comparisons must use consolidated numbers (the daily bar) instead -- the
analyzer does exactly that. DataFrames are tagged df.attrs["source"] with
"alpaca" or "yfinance" so callers can tell.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

from .broker import _config

DATA_URL = "https://data.alpaca.markets"
NY = ZoneInfo("America/New_York")


def enabled() -> bool:
    cfg = _config()
    return bool(cfg["key_id"] and cfg["secret_key"])


def _get(path: str, params: dict) -> dict | None:
    cfg = _config()
    if not (cfg["key_id"] and cfg["secret_key"]):
        return None
    try:
        resp = requests.get(
            DATA_URL + path,
            params=params,
            headers={
                "APCA-API-KEY-ID": cfg["key_id"],
                "APCA-API-SECRET-KEY": cfg["secret_key"],
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        print(f"[data] alpaca request failed ({path}): {exc}")
        return None


def _alpaca_symbol(symbol: str) -> str | None:
    """Map to Alpaca naming; None when Alpaca cannot serve it."""
    if symbol.startswith("^"):
        return None  # cash indices (^GSPC) are not equities
    return symbol.replace("-", ".")  # BRK-B (yahoo) -> BRK.B


def _yf_intraday(symbol: str) -> pd.DataFrame:
    try:
        df = yf.Ticker(symbol).history(period="1d", interval="5m", auto_adjust=True)
    except Exception:
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_convert("America/New_York").tz_localize(None)
    df = df.dropna(subset=["Close"])
    today = datetime.now(NY).strftime("%Y-%m-%d")
    df = df[df.index.strftime("%Y-%m-%d") == today]
    df.attrs["source"] = "yfinance"
    return df


def intraday(symbol: str, yf_symbol: str | None = None) -> pd.DataFrame:
    """Today's regular-session 5-minute bars, tz-naive ET index.

    Alpaca IEX first (real-time), yfinance fallback. `yf_symbol` overrides
    the Yahoo name when it differs (e.g. SPX -> ^GSPC).
    """
    yf_sym = yf_symbol or symbol
    ap_sym = _alpaca_symbol(symbol)
    if ap_sym is None:
        return _yf_intraday(yf_sym)

    now = datetime.now(NY)
    start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now < start:  # pre-market: yesterday's session is the latest complete one
        return _yf_intraday(yf_sym)
    data = _get(
        f"/v2/stocks/{ap_sym}/bars",
        {
            "timeframe": "5Min",
            "start": start.isoformat(),
            "end": now.isoformat(),
            "limit": 10000,
            "feed": "iex",
            "adjustment": "raw",
        },
    )
    bars = (data or {}).get("bars") or []
    if not bars:
        return _yf_intraday(yf_sym)

    df = pd.DataFrame(bars).rename(columns={
        "o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"
    })
    idx = pd.to_datetime(df["t"], utc=True).dt.tz_convert("America/New_York")
    df.index = idx.dt.tz_localize(None)
    df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
    df = df[(df.index.strftime("%H:%M") >= "09:30") & (df.index.strftime("%H:%M") < "16:00")]
    df = df.dropna(subset=["Close"])
    if df.empty:
        return _yf_intraday(yf_sym)
    df.attrs["source"] = "alpaca"
    return df


def latest_price(symbol: str) -> float | None:
    """Latest trade price from Alpaca IEX; None when unavailable."""
    ap_sym = _alpaca_symbol(symbol)
    if ap_sym is None:
        return None
    data = _get(f"/v2/stocks/{ap_sym}/trades/latest", {"feed": "iex"})
    try:
        return float(data["trade"]["p"])
    except (TypeError, KeyError, ValueError):
        return None
