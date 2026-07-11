"""Swing Trade Tracker CLI.

Commands:
  scan      Screen a watchlist for RSI(2) pullback buy signals today.
  backtest  Verify the strategy's historical win rate and expectancy.
"""

import argparse

from swingtrader import backtest, screener
from swingtrader.data import read_watchlist


def _tickers(args: argparse.Namespace) -> list[str]:
    if args.tickers:
        return [t.upper() for t in args.tickers]
    return read_watchlist(args.watchlist)


def main() -> None:
    parser = argparse.ArgumentParser(description="Swing Trade Tracker: RSI(2) mean-reversion strategy")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="scan watchlist for buy signals today")
    p_scan.add_argument("--tickers", nargs="+", help="tickers to scan (default: watchlist.txt)")
    p_scan.add_argument("--watchlist", default="watchlist.txt")

    p_bt = sub.add_parser("backtest", help="backtest the strategy and report win rate")
    p_bt.add_argument("--tickers", nargs="+", help="tickers to test (default: watchlist.txt)")
    p_bt.add_argument("--watchlist", default="watchlist.txt")
    p_bt.add_argument("--years", type=int, default=5, help="years of history (default: 5)")

    args = parser.parse_args()
    tickers = _tickers(args)

    if args.command == "scan":
        print(f"Scanning {len(tickers)} tickers for RSI(2) pullback setups...\n")
        screener.print_signals(screener.scan(tickers))
    elif args.command == "backtest":
        print(f"Backtesting {len(tickers)} tickers over {args.years} years...\n")
        results = backtest.run(tickers, years=args.years)
        print("\n=== Results ===")
        print(results.summary())


if __name__ == "__main__":
    main()
