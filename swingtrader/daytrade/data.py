"""5-minute and daily bars for the day-trade universe, cached on disk.

yfinance serves only ~60 days of 5-minute bars. Every download is merged
into a per-ticker cache file, so history keeps growing past that window as
long as the cache is refreshed at least every ~50 days.
"""

import os
from pathlib import Path

import pandas as pd
import yfinance as yf

CACHE_DIR = Path(os.environ.get("DAYTRADE_CACHE", ".cache/daytrade"))
INTRADAY_PERIOD = "60d"
DAILY_PERIOD = "6mo"
BATCH = 50  # tickers per yf.download call
OHLCV = ["Open", "High", "Low", "Close", "Volume"]


def _cache_path(ticker: str, interval: str) -> Path:
    return CACHE_DIR / f"{ticker}_{interval}.pkl"


def _is_fresh(path: Path) -> bool:
    """A cache file is fresh when it was written today (local time)."""
    if not path.exists():
        return False
    written = pd.Timestamp.fromtimestamp(path.stat().st_mtime).normalize()
    return written == pd.Timestamp.today().normalize()


def _normalize(df: pd.DataFrame, intraday: bool) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=OHLCV)
    df = df.loc[:, ~df.columns.duplicated()]
    missing = [c for c in OHLCV if c not in df.columns]
    if missing:
        return pd.DataFrame(columns=OHLCV)
    df = df[OHLCV]
    if getattr(df.index, "tz", None) is not None:
        if intraday:
            df.index = df.index.tz_convert("America/New_York").tz_localize(None)
        else:
            df.index = df.index.tz_localize(None)
    return df.dropna(subset=["Close"])


def _split_batch(raw: pd.DataFrame, tickers: list[str]) -> dict[str, pd.DataFrame]:
    """yf.download(group_by='ticker') returns (ticker, field) columns."""
    out = {}
    if raw is None or raw.empty:
        return out
    if not isinstance(raw.columns, pd.MultiIndex):
        return {tickers[0]: raw} if len(tickers) == 1 else out
    level0 = set(raw.columns.get_level_values(0))
    for t in tickers:
        if t in level0:
            out[t] = raw[t]
    return out


def _download(tickers: list[str], interval: str, period: str) -> dict[str, pd.DataFrame]:
    """Batched download. A single sequential yf.download per batch avoids the
    duplicated-column issues yfinance has under concurrent calls."""
    out = {}
    for i in range(0, len(tickers), BATCH):
        chunk = tickers[i : i + BATCH]
        try:
            raw = yf.download(
                chunk,
                period=period,
                interval=interval,
                group_by="ticker",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
        except Exception as exc:  # network / rate limit
            print(f"  ! {interval} download failed for {len(chunk)} tickers: {exc}")
            continue
        out.update(_split_batch(raw, chunk))
    return out


def load_bars(tickers: list[str], interval: str = "5m", refresh: bool = True) -> dict[str, pd.DataFrame]:
    """Return {ticker: OHLCV frame}; intraday index is naive America/New_York.

    Cached tickers written today are reused; the rest are downloaded and
    merged into their cache file. With refresh=False only the cache is read.
    """
    intraday = interval != "1d"
    period = INTRADAY_PERIOD if intraday else DAILY_PERIOD
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    cached: dict[str, pd.DataFrame] = {}
    for t in tickers:
        path = _cache_path(t, interval)
        if path.exists():
            cached[t] = pd.read_pickle(path)

    stale = [t for t in tickers if refresh and not _is_fresh(_cache_path(t, interval))]
    if stale:
        print(f"  downloading {interval} bars for {len(stale)} tickers...")
        fresh = _download(stale, interval, period)
        for t, df in fresh.items():
            df = _normalize(df, intraday)
            if df.empty:
                continue
            if t in cached and not cached[t].empty:
                df = pd.concat([cached[t], df])
                df = df[~df.index.duplicated(keep="last")].sort_index()
            df.to_pickle(_cache_path(t, interval))
            cached[t] = df

    return {t: df for t, df in cached.items() if not df.empty}


def regular_session(df: pd.DataFrame) -> pd.DataFrame:
    """Keep 09:30-15:55 bar starts (the regular session in 5-minute bars)."""
    if df.empty:
        return df
    t = df.index.time
    return df[(t >= pd.Timestamp("09:30").time()) & (t <= pd.Timestamp("15:55").time())]
