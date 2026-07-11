"""Swing Trade Tracker CLI.

The web UI (python webapp.py) is the primary interface; this CLI mirrors it.

Commands:
  scan       Screen a watchlist for buy signals across strategies today.
  backtest   Backtest strategies and print results per strategy.
  dashboard  Backtest all strategies and write a static HTML dashboard file.
"""

import argparse

from swingtrader import backtest, dashboard, screener
from swingtrader.data import parse_duration, read_watchlist
from swingtrader.strategies import STRATEGIES


def _tickers(args: argparse.Namespace) -> list[str]:
    if args.tickers:
        return [t.upper() for t in args.tickers]
    return read_watchlist(args.watchlist)


def _strategies(args: argparse.Namespace) -> list:
    if args.strategy == "all":
        return list(STRATEGIES.values())
    return [STRATEGIES[args.strategy]]


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--tickers", nargs="+", help="tickers to use (default: watchlist.txt)")
    p.add_argument("--watchlist", default="watchlist.txt")
    p.add_argument("--strategy", default="all", choices=["all", *STRATEGIES])
    p.add_argument(
        "--earnings-buffer",
        type=int,
        default=3,
        help="days around earnings to avoid trading (default: 3)",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Swing Trade Tracker: multi-strategy screener, backtester, and dashboard"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="scan watchlist for buy signals today")
    _add_common(p_scan)

    p_bt = sub.add_parser("backtest", help="backtest strategies and print results")
    _add_common(p_bt)
    p_bt.add_argument("--duration", default="5y", help="window, e.g. 1m, 6m, 2y (default: 5y)")

    p_dash = sub.add_parser("dashboard", help="backtest all strategies and write dashboard.html")
    _add_common(p_dash)
    p_dash.add_argument("--duration", default="5y", help="window, e.g. 1m, 6m, 2y (default: 5y)")
    p_dash.add_argument("--output", default="dashboard.html")

    args = parser.parse_args()
    tickers = _tickers(args)
    strategies = _strategies(args)

    if args.command == "scan":
        print(f"Scanning {len(tickers)} tickers across {len(strategies)} strategies...\n")
        signals = screener.scan(tickers, strategies, earnings_buffer=args.earnings_buffer)
        screener.print_signals(signals)
        return

    duration_days = parse_duration(args.duration)
    print(
        f"Backtesting {len(strategies)} strategies over {len(tickers)} tickers, "
        f"{args.duration}, earnings buffer +/-{args.earnings_buffer} days...\n"
    )
    results = backtest.run_all(
        tickers, strategies, duration_days=duration_days, earnings_buffer=args.earnings_buffer
    )
    for res in results.values():
        print(f"\n=== {res.strategy} ===")
        print(res.summary())

    if args.command == "dashboard":
        meta = (
            f"{len(tickers)} tickers | {args.duration} | "
            f"earnings buffer +/-{args.earnings_buffer} days"
        )
        path = dashboard.generate(results, path=args.output, meta=meta)
        print(f"\nDashboard written to {path} - open it in a browser.")


if __name__ == "__main__":
    main()
