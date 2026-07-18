"""Submit a manual paper order (or check the account) from the command line.

Reuses the credentials and request plumbing in swingtrader/broker.py, so it
talks to the same Alpaca PAPER account the app trades on -- keys come from
alpaca.json or the APCA_API_* env vars, never live trading.

Examples:
    # Account snapshot: cash, portfolio value, buying power
    python paper_order.py account

    # Limit buy, extended-hours enabled (fills now if inside pre/post market)
    python paper_order.py buy AAPL --qty 1 --limit 210.00 --extended

    # Plain market buy -- queues after hours, fills at the next open
    python paper_order.py buy AAPL --qty 1

    # Sell (short an ETF / close a long)
    python paper_order.py sell QQQ --qty 2 --limit 480

    # List open orders / current positions
    python paper_order.py orders
    python paper_order.py positions

Nothing is sent without an explicit buy/sell subcommand; account/orders/
positions are read-only.
"""

import argparse
import json
import sys

from swingtrader import broker


def _cfg_or_exit() -> dict:
    cfg = broker._config()
    if not (cfg["key_id"] and cfg["secret_key"]):
        sys.exit(
            "No Alpaca credentials found. Create alpaca.json next to webapp.py "
            "(see alpaca.example.json) or set APCA_API_KEY_ID / APCA_API_SECRET_KEY."
        )
    return cfg


def _show(label: str, data) -> None:
    print(f"=== {label} ===")
    print(json.dumps(data, indent=2))


def cmd_account(cfg: dict) -> None:
    acct = broker._request("GET", "/v2/account", cfg)
    print("Account (paper):")
    for k in ("status", "cash", "portfolio_value", "equity", "buying_power",
              "long_market_value", "daytrade_count"):
        if k in acct:
            print(f"  {k:20} {acct[k]}")


def cmd_orders(cfg: dict) -> None:
    orders = broker._request(
        "GET", "/v2/orders?status=open&limit=50&nested=true", cfg
    )
    if not orders:
        print("No open orders.")
        return
    for o in orders:
        print(f"  {o['symbol']:6} {o['side']:4} {o['type']:8} "
              f"qty={o.get('qty')} status={o['status']} "
              f"limit={o.get('limit_price')} id={o['id'][:8]}")


def cmd_positions(cfg: dict) -> None:
    positions = broker._request("GET", "/v2/positions", cfg)
    if not positions:
        print("No open positions.")
        return
    for p in positions:
        print(f"  {p['symbol']:6} qty={p['qty']:>6} "
              f"avg={float(p['avg_entry_price']):.2f} "
              f"cur={float(p['current_price']):.2f} "
              f"P/L=${float(p['unrealized_pl']):.2f} "
              f"({100*float(p['unrealized_plpc']):.2f}%)")


def cmd_order(cfg: dict, args: argparse.Namespace) -> None:
    symbol = args.symbol.upper().replace("-", ".")  # BRK-B -> BRK.B
    payload = {
        "symbol": symbol,
        "qty": str(args.qty),
        "side": args.side,
        "type": "limit" if args.limit is not None else "market",
        "time_in_force": args.tif,
    }
    if args.limit is not None:
        payload["limit_price"] = f"{args.limit}"
    if args.extended:
        # Alpaca only accepts extended_hours on a limit + day order.
        payload["extended_hours"] = True
        if payload["type"] != "limit":
            sys.exit("--extended requires --limit (extended hours needs a limit price).")
        if args.tif != "day":
            sys.exit("--extended requires --tif day.")

    print("Submitting order:")
    print(json.dumps(payload, indent=2))
    try:
        order = broker._request("POST", "/v2/orders", cfg, json=payload)
    except Exception as exc:  # requests.HTTPError etc.
        resp = getattr(exc, "response", None)
        detail = resp.text if resp is not None else str(exc)
        sys.exit(f"Order rejected: {detail}")
    print("\nAccepted:")
    print(f"  id       {order['id']}")
    print(f"  status   {order['status']}")
    print(f"  symbol   {order['symbol']}  {order['side']} {order['qty']} @ "
          f"{order.get('limit_price') or 'market'}")
    print("\nTip: run 'python paper_order.py account' before/after the fill to "
          "see the cash & buying-power delta.")


def main() -> None:
    p = argparse.ArgumentParser(description="Manual Alpaca paper orders.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("account", help="show cash / portfolio value / buying power")
    sub.add_parser("orders", help="list open orders")
    sub.add_parser("positions", help="list current positions")

    for side in ("buy", "sell"):
        sp = sub.add_parser(side, help=f"submit a {side} order")
        sp.add_argument("symbol")
        sp.add_argument("--qty", type=int, default=1, help="whole shares (default 1)")
        sp.add_argument("--limit", type=float, default=None,
                        help="limit price; omit for a market order")
        sp.add_argument("--tif", default="day",
                        choices=["day", "gtc", "opg", "cls", "ioc", "fok"],
                        help="time in force (default day)")
        sp.add_argument("--extended", action="store_true",
                        help="allow extended-hours fill (needs --limit and --tif day)")

    args = p.parse_args()
    cfg = _cfg_or_exit()

    if args.cmd == "account":
        cmd_account(cfg)
    elif args.cmd == "orders":
        cmd_orders(cfg)
    elif args.cmd == "positions":
        cmd_positions(cfg)
    else:  # buy / sell
        args.side = args.cmd
        cmd_order(cfg, args)


if __name__ == "__main__":
    main()
