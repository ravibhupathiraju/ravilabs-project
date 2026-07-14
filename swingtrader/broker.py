"""Alpaca paper-trading bridge.

Mirrors the swing alert lifecycle onto a free Alpaca paper account so live
signals become real (simulated) orders with broker-side fills:

  swing SIGNAL alert  -> bracket market order, GTC. Submitted after the close
                         it queues and fills at the next open -- exactly the
                         backtest's "fill at next open" model. The ATR stop
                         (and target, when the strategy has one) ride along as
                         server-side legs, so they trigger even if this app is
                         offline.
  swing EXIT alert    -> cancel the legs and close the position at market
                         (no-op if a bracket leg already closed it).

Config: create ``alpaca.json`` next to webapp.py (see alpaca.example.json),
or set the standard APCA_API_KEY_ID / APCA_API_SECRET_KEY environment
variables. No config -> every function is a quiet no-op, so the rest of the
app works unchanged. Keys come from a PAPER account at https://alpaca.markets
(Dashboard -> switch to Paper -> API keys); this module always talks to the
paper endpoint, never live trading.

One broker position per symbol: if two strategies signal the same ticker,
the first alert owns the position and later ones are skipped.
"""

import json
import os
from pathlib import Path

import requests

CONFIG_PATH = Path(__file__).resolve().parent.parent / "alpaca.json"
PAPER_URL = "https://paper-api.alpaca.markets"
DEFAULT_NOTIONAL = 10_000.0  # $ per trade when the config does not say


def _config() -> dict:
    cfg = {
        "key_id": os.environ.get("APCA_API_KEY_ID", ""),
        "secret_key": os.environ.get("APCA_API_SECRET_KEY", ""),
        "notional_per_trade": DEFAULT_NOTIONAL,
    }
    if CONFIG_PATH.exists():
        try:
            file_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[broker] cannot read {CONFIG_PATH.name}: {exc}")
            file_cfg = {}
        for k in cfg:
            if file_cfg.get(k):
                cfg[k] = file_cfg[k]
    cfg["notional_per_trade"] = float(cfg["notional_per_trade"])
    return cfg


def enabled() -> bool:
    cfg = _config()
    return bool(cfg["key_id"] and cfg["secret_key"])


def _request(method: str, path: str, cfg: dict, **kwargs):
    """One API call; returns parsed JSON or raises requests.HTTPError."""
    resp = requests.request(
        method,
        PAPER_URL + path,
        headers={
            "APCA-API-KEY-ID": cfg["key_id"],
            "APCA-API-SECRET-KEY": cfg["secret_key"],
        },
        timeout=15,
        **kwargs,
    )
    resp.raise_for_status()
    return resp.json() if resp.text else {}


def status() -> dict:
    """Account, positions and open orders for the UI. Never raises."""
    cfg = _config()
    if not (cfg["key_id"] and cfg["secret_key"]):
        return {"enabled": False}
    try:
        acct = _request("GET", "/v2/account", cfg)
        positions = _request("GET", "/v2/positions", cfg)
        orders = _request("GET", "/v2/orders?status=open&limit=50&nested=true", cfg)
    except Exception as exc:
        return {"enabled": True, "error": str(exc)}
    return {
        "enabled": True,
        "account": {
            "equity": float(acct["equity"]),
            "cash": float(acct["cash"]),
            "buying_power": float(acct["buying_power"]),
        },
        "positions": [
            {
                "symbol": p["symbol"],
                "qty": float(p["qty"]),
                "avg_entry": float(p["avg_entry_price"]),
                "current": float(p["current_price"]),
                "unrealized_pl": float(p["unrealized_pl"]),
                "unrealized_plpc": 100.0 * float(p["unrealized_plpc"]),
            }
            for p in positions
        ],
        "open_orders": [
            {
                "symbol": o["symbol"],
                "side": o["side"],
                "type": o["type"],
                "qty": o.get("qty"),
                "status": o["status"],
                "submitted_at": o["submitted_at"][:19],
            }
            for o in orders
        ],
        "notional_per_trade": cfg["notional_per_trade"],
    }


def _already_holding(symbol: str, cfg: dict) -> bool:
    try:
        _request("GET", f"/v2/positions/{symbol}", cfg)
        return True  # 200 = position exists
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            pass  # no position; check pending orders too
        else:
            raise
    orders = _request("GET", f"/v2/orders?status=open&symbols={symbol}", cfg)
    return len(orders) > 0


def buy_bracket(symbol: str, price: float, stop: float, target: float | None):
    """Market buy with a server-side stop (and target when given).

    Returns the Alpaca order id, or None when disabled / skipped / failed.
    Whole shares sized from notional_per_trade (bracket orders cannot be
    fractional); GTC so an after-hours submit fills at the next open.
    """
    cfg = _config()
    if not (cfg["key_id"] and cfg["secret_key"]):
        return None
    symbol = symbol.replace("-", ".")  # BRK-B (yahoo) -> BRK.B (alpaca)
    try:
        if _already_holding(symbol, cfg):
            print(f"[broker] {symbol}: already held/pending; skipped")
            return None
        qty = int(cfg["notional_per_trade"] / price)
        if qty < 1:
            print(f"[broker] {symbol}: price {price:.2f} above notional; skipped")
            return None
        payload = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "buy",
            "type": "market",
            "time_in_force": "gtc",
            "order_class": "bracket" if target else "oto",
            "stop_loss": {"stop_price": f"{stop:.2f}"},
        }
        if target:
            payload["take_profit"] = {"limit_price": f"{target:.2f}"}
        order = _request("POST", "/v2/orders", cfg, json=payload)
        print(f"[broker] {symbol}: paper buy x{qty} submitted (order {order['id'][:8]})")
        return order["id"]
    except Exception as exc:
        print(f"[broker] {symbol}: order failed: {exc}")
        return None


def close(symbol: str) -> bool:
    """Cancel open orders on the symbol and close the position at market.

    Safe to call when the position is already flat (bracket leg filled):
    Alpaca answers 404 and this returns False quietly.
    """
    cfg = _config()
    if not (cfg["key_id"] and cfg["secret_key"]):
        return False
    symbol = symbol.replace("-", ".")
    try:
        _request("DELETE", f"/v2/positions/{symbol}?cancel_orders=true", cfg)
        print(f"[broker] {symbol}: paper position closed")
        return True
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return False  # already flat
        print(f"[broker] {symbol}: close failed: {exc}")
        return False
    except Exception as exc:
        print(f"[broker] {symbol}: close failed: {exc}")
        return False
