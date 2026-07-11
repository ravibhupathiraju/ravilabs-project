"""Scan a watchlist for stocks triggering the strategy's buy signal today."""

from .data import load_history
from .strategy import Signal, latest_signal


def scan(tickers: list[str], years: int = 2) -> list[Signal]:
    """Return today's buy signals across the watchlist, best (lowest RSI) first."""
    signals: list[Signal] = []
    for ticker in tickers:
        df = load_history(ticker, years=years)
        if df.empty:
            continue
        sig = latest_signal(ticker, df)
        if sig:
            signals.append(sig)
    # Lower RSI(2) = deeper pullback = historically better expectancy.
    signals.sort(key=lambda s: s.rsi2)
    return signals


def print_signals(signals: list[Signal]) -> None:
    if not signals:
        print("No buy signals today. Mean-reversion setups need a pullback; check again tomorrow.")
        return
    print(f"{'TICKER':<8}{'DATE':<12}{'CLOSE':>10}{'RSI(2)':>8}{'SMA5':>10}{'STOP':>10}")
    print("-" * 58)
    for s in signals:
        print(
            f"{s.ticker:<8}{s.date:<12}{s.close:>10.2f}{s.rsi2:>8.1f}"
            f"{s.sma5:>10.2f}{s.stop_price:>10.2f}"
        )
    print(f"\nExit plan: {signals[0].target_note}")
