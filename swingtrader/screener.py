"""Scan a watchlist for stocks triggering any strategy's buy signal today."""

from .data import load_history
from .earnings import earnings_dates, near_earnings
from .strategies import Strategy


def scan(
    tickers: list[str],
    strategies: list[Strategy],
    years: int = 2,
    earnings_buffer: int = 3,
) -> list[dict]:
    """Return today's buy signals across all strategies, earnings-filtered."""
    signals: list[dict] = []
    for ticker in tickers:
        df = load_history(ticker, years=years)
        if df.empty:
            continue
        row = df.iloc[-1]
        date = df.index[-1]
        edates = earnings_dates(ticker)
        if near_earnings(date, edates, earnings_buffer):
            print(f"  - {ticker}: skipped (within earnings window)")
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
