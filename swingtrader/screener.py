"""Scan tickers for stocks triggering any strategy's buy signal today."""

from concurrent.futures import ThreadPoolExecutor

from .data import load_history
from .earnings import earnings_dates, near_earnings
from .strategies import Strategy

HISTORY_DAYS = 420  # enough calendar days for 200-day SMA warmup


def _load(ticker: str):
    df = load_history(ticker, days=HISTORY_DAYS)
    edates = earnings_dates(ticker) if not df.empty else []
    return ticker, df, edates


def scan(
    tickers: list[str],
    strategies: list[Strategy],
    earnings_buffer: int = 3,
    max_workers: int = 8,
) -> list[dict]:
    """Return today's buy signals across all strategies, earnings-filtered."""
    signals: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for ticker, df, edates in pool.map(_load, tickers):
            if df.empty:
                continue
            row = df.iloc[-1]
            date = df.index[-1]
            if near_earnings(date, edates, earnings_buffer):
                continue
            for s in strategies:
                if s.entry(row):
                    signals.append(
                        {
                            "ticker": ticker,
                            "strategy": s.label,
                            "date": str(date.date()),
                            "close": float(row["Close"]),
                            "stop": s.stop_price(row),
                        }
                    )
    signals.sort(key=lambda s: (s["strategy"], s["ticker"]))
    return signals


def print_signals(signals: list[dict]) -> None:
    if not signals:
        print("No buy signals today. Check again tomorrow.")
        return
    print(f"{'TICKER':<8}{'STRATEGY':<26}{'DATE':<12}{'CLOSE':>10}{'STOP':>10}")
    print("-" * 66)
    for s in signals:
        print(
            f"{s['ticker']:<8}{s['strategy']:<26}{s['date']:<12}"
            f"{s['close']:>10.2f}{s['stop']:>10.2f}"
        )
