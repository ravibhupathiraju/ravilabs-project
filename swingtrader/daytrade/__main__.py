"""CLI: python -m swingtrader.daytrade [--tickers ...] [--csv signals.csv]"""

import argparse

import pandas as pd

from . import run_backtest
from .report import STOP_PCT, TARGET_PCT


def _table(rows: list[dict], first: str, cols: list[str]) -> None:
    if not rows:
        print("  (none)")
        return
    df = pd.DataFrame(rows)
    cols = [c for c in [first, *cols] if c in df.columns]
    print(df[cols].to_string(index=False))


def main() -> None:
    p = argparse.ArgumentParser(description="Backtest the intraday RS day-trade scanner")
    p.add_argument("--tickers", nargs="+", help="override the built-in universe")
    p.add_argument("--target", type=float, default=TARGET_PCT, help="take-profit %% of stock move")
    p.add_argument("--stop", type=float, default=STOP_PCT, help="stop-loss %% of stock move")
    p.add_argument("--cached", action="store_true", help="use cached bars only, no download")
    p.add_argument("--wide-bands", action="store_true",
                   help="trigger at iRS >= 90 / <= 10 instead of 95 / 5")
    p.add_argument("--csv", help="write every signal to this CSV file")
    args = p.parse_args()

    res = run_backtest(args.tickers, target=args.target, stop=args.stop, refresh=not args.cached,
                       wide_bands=args.wide_bands)
    print(f"\n{res['sessions']} sessions, {res['signals']} signals, "
          f"{res['tickers_with_data']}/{res['tickers_requested']} tickers with data, "
          f"exit rule +{res['target_pct']}% / -{res['stop_pct']}% else close\n")
    cols = ["n", "hit_pct", "avg_mfe", "avg_mae", "avg_close", "sim_win_pct", "sim_expectancy", "sim_profit_factor"]
    print("RS PROFILE")
    _table(res["rs_profile"], "group", cols)
    print("\nBY TIME OF FIRST SIGNAL")
    _table(res["by_time"], "group", cols)
    print("\nFILTERS")
    _table(res["filters"], "label", ["side", *cols])
    if args.csv:
        pd.DataFrame([s.to_dict() for s in res["_signals"]]).to_csv(args.csv, index=False)
        print(f"\nwrote {len(res['_signals'])} signals to {args.csv}")


if __name__ == "__main__":
    main()
