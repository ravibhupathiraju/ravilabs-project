"""Intraday relative-strength day-trade scanner and backtester.

Kept separate from the swing and 0DTE modules. Entry points:
    run_backtest()                     -> report dict (used by the web page)
    python -m swingtrader.daytrade     -> CLI
"""

from . import data, report, scanner
from .universe import UNIVERSE


def run_backtest(tickers=None, filters=None, target=report.TARGET_PCT,
                 stop=report.STOP_PCT, refresh=True, wide_bands=False) -> dict:
    tickers = [t.upper() for t in (tickers or UNIVERSE)]
    intraday = data.load_bars(tickers, "5m", refresh=refresh)
    daily = data.load_bars(tickers, "1d", refresh=refresh)
    signals = scanner.replay(intraday, daily, wide_bands=wide_bands)
    out = report.build(signals, filters, target, stop)
    out["tickers_requested"] = len(tickers)
    out["tickers_with_data"] = len(intraday)
    out["wide_bands"] = wide_bands
    out["_signals"] = signals  # for CSV export; stripped before JSON
    return out
