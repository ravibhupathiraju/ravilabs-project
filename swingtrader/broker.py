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
import re
import uuid
from pathlib import Path

import requests

CONFIG_PATH = Path(__file__).resolve().parent.parent / "alpaca.json"
CHANNELS_PATH = Path(__file__).resolve().parent.parent / "broker_channels.json"
PAPER_URL = "https://paper-api.alpaca.markets"

# Per-tab kill switches for paper-trade ENTRIES. A paused channel still
# alerts and logs exactly as before -- signals just never reach the broker.
# Exits (close/flatten) are never gated, so pausing cannot strand an open
# position. Persisted to broker_channels.json so the choice survives restarts.
# Every strategy trades by default; pause from any tab's pill. Each has its
# own dedicated Alpaca paper account (accounts.<name> in alpaca.json), so
# results never mix and two strategies can hold the same symbol at once.
DEFAULT_CHANNELS = {"swing": True, "zerodte": True, "tvtester": True, "pattern": True}

# Portfolio guardrails. A single scan of the full universe can produce 40+
# simultaneous signals; without caps they would all be sent, blow past the
# account's overnight buying power (2x equity under Reg T) and get rejected
# in a race. These bound what any one day can commit.
DEFAULT_NOTIONAL = 10_000.0   # $ per swing trade
DEFAULT_MAX_POSITIONS = 10    # concurrent paper positions (incl. pending buys)
DEFAULT_MAX_EXPOSURE_PCT = 100.0  # % of equity deployable; 100 = no leverage

# 0DTE: intraday, flat by the close. Only the strategies listed here are sent
# to the broker -- the rest still alert and log, they just do not trade.
DEFAULT_ZERODTE_NOTIONAL = 5_000.0
DEFAULT_ZERODTE_STRATEGIES = ["vwap"]

# SPX is a cash index (^GSPC): alertable, but not tradable as an equity.
# The others are ETFs and trade normally, long or short.
UNTRADABLE = {"SPX"}


def _config(channel: str | None = None) -> dict:
    """Settings plus the API keys for one strategy channel.

    Each channel (swing / zerodte / tvtester) trades on its OWN paper
    account, configured under ``accounts.<channel>`` in alpaca.json, so
    each strategy's equity curve and fill history stay cleanly separated.
    Channels without an entry fall back to the top-level keys.
    """
    cfg = {
        "key_id": os.environ.get("APCA_API_KEY_ID", ""),
        "secret_key": os.environ.get("APCA_API_SECRET_KEY", ""),
        "notional_per_trade": DEFAULT_NOTIONAL,
        "max_positions": DEFAULT_MAX_POSITIONS,
        "max_exposure_pct": DEFAULT_MAX_EXPOSURE_PCT,
        "zerodte_notional": DEFAULT_ZERODTE_NOTIONAL,
        "zerodte_strategies": list(DEFAULT_ZERODTE_STRATEGIES),
    }
    if CONFIG_PATH.exists():
        try:
            file_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[broker] cannot read {CONFIG_PATH.name}: {exc}")
            file_cfg = {}
        for k in cfg:
            if file_cfg.get(k) is not None and file_cfg.get(k) != "":
                cfg[k] = file_cfg[k]
        acct = (file_cfg.get("accounts") or {}).get(channel or "") or {}
        if acct.get("key_id") and acct.get("secret_key"):
            cfg["key_id"] = acct["key_id"]
            cfg["secret_key"] = acct["secret_key"]
    cfg["notional_per_trade"] = float(cfg["notional_per_trade"])
    cfg["max_positions"] = int(cfg["max_positions"])
    cfg["max_exposure_pct"] = float(cfg["max_exposure_pct"])
    cfg["zerodte_notional"] = float(cfg["zerodte_notional"])
    cfg["zerodte_strategies"] = [str(s) for s in cfg["zerodte_strategies"]]
    return cfg


def zerodte_strategies() -> list[str]:
    """Which 0DTE strategies are mirrored to the broker (default: VWAP only)."""
    return _config()["zerodte_strategies"]


def channels() -> dict:
    """Current active/paused state of every trading channel."""
    out = dict(DEFAULT_CHANNELS)
    if CHANNELS_PATH.exists():
        try:
            saved = json.loads(CHANNELS_PATH.read_text(encoding="utf-8"))
            out.update({k: bool(v) for k, v in saved.items() if k in out})
        except (OSError, ValueError) as exc:
            print(f"[broker] cannot read {CHANNELS_PATH.name}: {exc}")
    return out


def set_channel(name: str, active: bool) -> dict:
    """Flip one channel's entry gate and persist the whole map."""
    ch = channels()
    if name not in ch:
        raise ValueError(f"unknown channel {name!r}")
    ch[name] = bool(active)
    CHANNELS_PATH.write_text(json.dumps(ch, indent=2), encoding="utf-8")
    print(f"[broker] channel {name} -> {'ACTIVE' if active else 'PAUSED'}")
    return ch


def channel_active(name: str) -> bool:
    return channels().get(name, False)


def _tag(prefix: str, strategy: str | None) -> str:
    """Client-order-id carrying the strategy, e.g. ``sw_breakout_a1b2c3d4``.

    Performance analysis parses these back out of the broker's own order
    history, so attribution never depends on local state.
    """
    slug = re.sub(r"[^a-z0-9]+", "", (strategy or "x").lower())[:20] or "x"
    return f"{prefix}_{slug}_{uuid.uuid4().hex[:8]}"


def enabled(channel: str | None = None) -> bool:
    cfg = _config(channel)
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
    """Per-channel account/positions/orders for the UI. Never raises.

    One block per strategy channel, since each trades on its own paper
    account: {"channels": {...}, "swing": {...}, "zerodte": {...},
    "tvtester": {...}} -- each block in the classic single-account shape.
    """
    out: dict = {"channels": channels()}
    for ch in DEFAULT_CHANNELS:
        out[ch] = _channel_status(ch)
    return out


def _channel_status(channel: str) -> dict:
    cfg = _config(channel)
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
        "max_positions": cfg["max_positions"],
        "max_exposure_pct": cfg["max_exposure_pct"],
        "zerodte_notional": cfg["zerodte_notional"],
        "paper_0dte": cfg["zerodte_strategies"],
        "slots_used": len({p["symbol"] for p in positions}
                          | {o["symbol"] for o in orders if o["side"] == "buy"}),
        "exposure": sum(abs(float(p["market_value"])) for p in positions),
    }


def _portfolio(cfg: dict) -> dict:
    """Live snapshot used by the guardrails: what is already committed."""
    acct = _request("GET", "/v2/account", cfg)
    positions = _request("GET", "/v2/positions", cfg)
    orders = _request("GET", "/v2/orders?status=open&limit=200", cfg)

    held = {p["symbol"] for p in positions}
    exposure = sum(abs(float(p["market_value"])) for p in positions)
    pending = set()
    for o in orders:
        if o["side"] != "buy":
            continue
        pending.add(o["symbol"])
        qty = float(o.get("qty") or 0)
        # market orders carry no price; fall back to the stop leg or 0
        px = float(o.get("limit_price") or o.get("stop_price") or 0) or 0.0
        exposure += qty * px
    return {
        "equity": float(acct["equity"]),
        "held": held,
        "pending": pending,
        "slots_used": len(held | pending),
        "exposure": exposure,
    }


def buy_bracket(symbol: str, price: float, stop: float, target: float | None,
                strategy: str | None = None):
    """Market buy with a server-side stop (and target when given).

    Sent to the SWING channel's own paper account, tagged with a
    ``sw_<strategy>_`` client-order-id so performance analysis can
    attribute every fill to the strategy that generated it.

    Returns {"id", "qty", "notional"} on success, or None when disabled,
    skipped by a guardrail, or rejected. Whole shares sized from
    notional_per_trade (advanced order classes cannot be fractional);
    GTC so an after-hours submit fills at the next open.

    Guardrails, checked against the live account before every order:
      - one position per symbol (a held or pending symbol is skipped)
      - at most `max_positions` concurrent positions
      - total exposure kept under `max_exposure_pct` of equity
    """
    cfg = _config("swing")
    if not (cfg["key_id"] and cfg["secret_key"]):
        return None
    if not channel_active("swing"):
        print(f"[broker] {symbol}: swing paper trading paused; alert only")
        return None
    symbol = symbol.replace("-", ".")  # BRK-B (yahoo) -> BRK.B (alpaca)
    try:
        pf = _portfolio(cfg)

        if symbol in pf["held"] or symbol in pf["pending"]:
            print(f"[broker] {symbol}: already held/pending; skipped")
            return None
        if pf["slots_used"] >= cfg["max_positions"]:
            print(f"[broker] {symbol}: at position cap "
                  f"({pf['slots_used']}/{cfg['max_positions']}); skipped")
            return None

        qty = int(cfg["notional_per_trade"] / price)
        if qty < 1:
            print(f"[broker] {symbol}: price {price:.2f} above notional; skipped")
            return None
        notional = qty * price

        cap = pf["equity"] * cfg["max_exposure_pct"] / 100.0
        if pf["exposure"] + notional > cap:
            print(f"[broker] {symbol}: would exceed exposure cap "
                  f"(${pf['exposure']:,.0f} + ${notional:,.0f} > ${cap:,.0f}); skipped")
            return None

        payload = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "buy",
            "type": "market",
            "time_in_force": "gtc",
            "order_class": "bracket" if target else "oto",
            "stop_loss": {"stop_price": f"{stop:.2f}"},
            "client_order_id": _tag("sw", strategy),
        }
        if target:
            payload["take_profit"] = {"limit_price": f"{target:.2f}"}
        order = _request("POST", "/v2/orders", cfg, json=payload)
        print(f"[broker] {symbol}: paper buy {qty} shares (~${notional:,.0f}) "
              f"submitted (order {order['id'][:8]})")
        return {"id": order["id"], "qty": qty, "notional": notional}
    except Exception as exc:
        print(f"[broker] {symbol}: order failed: {exc}")
        return None


def zerodte_order(symbol: str, strategy: str, direction: str, price: float):
    """Intraday market order for a 0DTE signal (long or short).

    Unlike swing trades this is NOT a bracket: the VWAP exit is a moving
    level, not a fixed price, so the monitor closes the position when its
    exit alert fires (VWAP touch) and the end-of-session sweep flattens
    anything still open before 16:00. time_in_force='day' so nothing can
    survive the session by accident.

    Returns {"id", "qty", "notional", "side"} or None when disabled, not in
    zerodte_strategies, untradable (SPX), or blocked by a guardrail.
    """
    cfg = _config("zerodte")  # the 0DTE strategy's own paper account
    if not (cfg["key_id"] and cfg["secret_key"]):
        return None
    if not channel_active("zerodte"):
        print(f"[broker] {symbol}: 0DTE paper trading paused; alert only")
        return None
    if strategy not in cfg["zerodte_strategies"]:
        return None  # alert-only strategy: logged, never traded
    if symbol in UNTRADABLE:
        print(f"[broker] {symbol}: cash index, not tradable; alert only")
        return None

    side = "buy" if direction == "long" else "sell"   # sell = short the ETF
    try:
        pf = _portfolio(cfg)
        if symbol in pf["held"] or symbol in pf["pending"]:
            print(f"[broker] {symbol}: already held/pending; skipped")
            return None
        if pf["slots_used"] >= cfg["max_positions"]:
            print(f"[broker] {symbol}: at position cap "
                  f"({pf['slots_used']}/{cfg['max_positions']}); skipped")
            return None

        qty = int(cfg["zerodte_notional"] / price)
        if qty < 1:
            print(f"[broker] {symbol}: price {price:.2f} above notional; skipped")
            return None
        notional = qty * price

        cap = pf["equity"] * cfg["max_exposure_pct"] / 100.0
        if pf["exposure"] + notional > cap:
            print(f"[broker] {symbol}: would exceed exposure cap; skipped")
            return None

        order = _request("POST", "/v2/orders", cfg, json={
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": "market",
            "time_in_force": "day",   # never carries overnight
            "client_order_id": _tag("zd", strategy),
        })
        print(f"[broker] {symbol}: 0DTE {strategy} paper {side} {qty} shares "
              f"(~${notional:,.0f}) (order {order['id'][:8]})")
        return {"id": order["id"], "qty": qty, "notional": notional, "side": side}
    except Exception as exc:
        print(f"[broker] {symbol}: 0DTE order failed: {exc}")
        return None


def pattern_order(symbol: str, direction: str, price: float):
    """Intraday market order for a Pattern-lab live match (long or short).

    Runs on the Pattern channel's OWN paper account, so it never collides
    with 0DTE even on a shared symbol. Like 0DTE it is a plain day-market
    order -- the monitor closes it when the ±$ barrier is hit or at the
    session close, and the 15:55 sweep flattens any leftover, so nothing
    carries overnight. Tagged ``pt_`` for performance attribution.

    Returns {"id", "qty", "notional", "side"} or None when disabled, paused,
    untradable (SPX), or blocked by a guardrail.
    """
    cfg = _config("pattern")
    if not (cfg["key_id"] and cfg["secret_key"]):
        return None
    if not channel_active("pattern"):
        print(f"[broker] {symbol}: pattern paper trading paused; alert only")
        return None
    if symbol in UNTRADABLE:
        print(f"[broker] {symbol}: cash index, not tradable; alert only")
        return None

    side = "buy" if direction == "long" else "sell"
    try:
        pf = _portfolio(cfg)
        if symbol in pf["held"] or symbol in pf["pending"]:
            print(f"[broker] {symbol}: already held/pending on the pattern account; skipped")
            return None
        if pf["slots_used"] >= cfg["max_positions"]:
            print(f"[broker] {symbol}: at position cap "
                  f"({pf['slots_used']}/{cfg['max_positions']}); skipped")
            return None

        qty = int(cfg["zerodte_notional"] / price)
        if qty < 1:
            print(f"[broker] {symbol}: price {price:.2f} above notional; skipped")
            return None
        notional = qty * price

        cap = pf["equity"] * cfg["max_exposure_pct"] / 100.0
        if pf["exposure"] + notional > cap:
            print(f"[broker] {symbol}: would exceed exposure cap; skipped")
            return None

        order = _request("POST", "/v2/orders", cfg, json={
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": "market",
            "time_in_force": "day",   # never carries overnight
            "client_order_id": _tag("pt", "pattern"),
        })
        print(f"[broker] {symbol}: pattern paper {side} {qty} shares "
              f"(~${notional:,.0f}) (order {order['id'][:8]})")
        return {"id": order["id"], "qty": qty, "notional": notional, "side": side}
    except Exception as exc:
        print(f"[broker] {symbol}: pattern order failed: {exc}")
        return None


def flatten(symbols: list[str], channel: str = "zerodte") -> int:
    """Close any open paper position in `symbols` (0DTE end-of-session sweep).

    Runs on the channel's own account (0DTE by default). Since each strategy
    now trades on a separate account, the sweep can no longer collide with
    swing positions even for overlapping symbols. Returns the number of
    positions actually closed.
    """
    cfg = _config(channel)
    if not (cfg["key_id"] and cfg["secret_key"]):
        return 0
    try:
        positions = _request("GET", "/v2/positions", cfg)
    except Exception as exc:
        print(f"[broker] flatten failed: {exc}")
        return 0
    wanted = {s for s in symbols if s not in UNTRADABLE}
    closed = 0
    for p in positions:
        if p["symbol"] in wanted and close(p["symbol"], channel):
            closed += 1
    if closed:
        print(f"[broker] end-of-session sweep: flattened {closed} 0DTE position(s)")
    return closed


def close(symbol: str, channel: str = "swing") -> bool:
    """Cancel open orders on the symbol and close the position at market.

    Runs on the channel's own account. Safe to call when the position is
    already flat (bracket leg filled): Alpaca answers 404 and this returns
    False quietly.
    """
    cfg = _config(channel)
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
