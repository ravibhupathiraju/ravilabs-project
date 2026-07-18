"""TV strategy tester: turn written trade plans into Alpaca paper trades.

Workflow the tab implements:

  1. Paste a free-form recommendation (the kind you get from an analysis
     chat: entry zone, stop, targets). ``parse_notes`` extracts the levels
     with regex heuristics and pre-fills the plan form -- you review and
     correct before anything is sent. Nothing trades without your click.
  2. "Arm" submits ONE server-side bracket order to Alpaca: limit buy at
     the entry, with the stop-loss and take-profit riding along as legs.
     Alpaca then runs the whole plan even while this app is offline:
     fill in the pullback zone -> stop below -> target above.
  3. Manual one-off buys/sells are also supported (market or limit).

Every order this tab sends is tagged with a ``tvst_`` client-order-id
prefix, so the trades table and the closed-trade P/L report can be built
straight from Alpaca's own order history -- no local double-bookkeeping
of fills. Plans (the levels + reasoning) live in tv_plans.json.

Trading is gated on broker channel "tvtester" (see broker.channels), and
everything goes to the PAPER endpoint only.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from . import broker

NY = ZoneInfo("America/New_York")
PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tv_plans.json"
)
TAG = "tvst_"  # client_order_id prefix that scopes Alpaca history to this tab

# Uppercase tokens that look like tickers but are indicator/market jargon.
_NOT_TICKERS = {
    "ADX", "RSI", "EMA", "SMA", "ATR", "MACD", "VWAP", "OBV", "BB", "KC",
    "OTO", "GTC", "DAY", "IOC", "FOK", "OPG", "CLS", "ETF", "CPI", "FOMC",
    "NFP", "OPEX", "AI", "USD", "PM", "AM", "EST", "EDT", "NYSE", "LOW",
    "HIGH", "OPEN", "CLOSE", "WHY", "ACTION", "LEVEL", "STOP", "TARGET",
    "ENTER", "WAIT", "BUY", "SELL", "LONG", "SHORT", "THE", "AND", "FOR",
}

_NUM = r"\$?\s*(\d{1,6}(?:\.\d{1,4})?)"


def _load() -> dict:
    if not os.path.exists(PATH):
        return {"seq": 0, "plans": []}
    try:
        with open(PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        d.setdefault("seq", 0)
        d.setdefault("plans", [])
        return d
    except Exception as exc:
        print(f"[tvtester] could not read {PATH}: {exc}")
        return {"seq": 0, "plans": []}


def _save(d: dict) -> None:
    with open(PATH, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=2)


# ---------------------------------------------------------------- parsing

def parse_notes(text: str) -> dict:
    """Extract ticker + levels from a written recommendation.

    Heuristics tuned for the usual shape of these notes: an entry/pullback
    zone given as a range, a stop below some number, one or two targets.
    Returns every field it could find plus ``warnings`` for what it
    couldn't -- the UI shows the result for review, it is never traded
    blindly.
    """
    out: dict = {"ticker": None, "entry_low": None, "entry_high": None,
                 "stop": None, "target": None, "target2": None, "warnings": []}
    if not text or not text.strip():
        out["warnings"].append("empty notes")
        return out
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # Ticker: prefer "for XYZ", else the most frequent standalone caps token.
    m = re.search(r"\bfor\s+([A-Z]{1,5})\b", text)
    if m and m.group(1) not in _NOT_TICKERS:
        out["ticker"] = m.group(1)
    else:
        counts: dict[str, int] = {}
        for tok in re.findall(r"\b[A-Z]{2,5}\b", text):
            if tok not in _NOT_TICKERS:
                counts[tok] = counts.get(tok, 0) + 1
        if counts:
            out["ticker"] = max(counts, key=lambda k: counts[k])

    def nums(s: str) -> list[float]:
        return [float(x) for x in re.findall(_NUM, s)]

    for ln in lines:
        low = ln.lower()
        # entry zone: a range on a line mentioning pullback/entry/zone/pivot
        if out["entry_low"] is None and re.search(
                r"pullback|entry|zone|pivot|enter", low):
            rng = re.search(_NUM + r"\s*[\-–—]\s*" + _NUM, ln)
            if rng:
                a, b = float(rng.group(1)), float(rng.group(2))
                out["entry_low"], out["entry_high"] = min(a, b), max(a, b)
        # "bounce off $82.90+" style single-level entry trigger
        if out["entry_low"] is None and "bounce" in low:
            n = nums(ln)
            if n:
                out["entry_low"] = out["entry_high"] = n[0]
        # read numbers only AFTER each keyword, so "Stop below 80. Target 95"
        # on one line assigns 80 to the stop and 95 to the target
        if "stop" in low and out["stop"] is None:
            n = nums(ln[low.index("stop"):])
            if n:
                out["stop"] = n[0]
        if "target" in low and out["target"] is None:
            n = nums(ln[low.index("target"):])
            if n:
                out["target"] = n[0]
                if len(n) > 1:
                    out["target2"] = n[1]

    # sanity ordering: stop < entry < target for a long
    if out["entry_high"] and out["stop"] and out["stop"] >= out["entry_high"]:
        out["warnings"].append("stop is not below the entry zone -- check levels")
    if out["entry_high"] and out["target"] and out["target"] <= out["entry_high"]:
        out["warnings"].append("target is not above the entry zone -- check levels")
    for k in ("ticker", "entry_high", "stop", "target"):
        if not out[k]:
            out["warnings"].append(f"could not find {k.replace('_', ' ')}")
    return out


def parse(text: str) -> dict:
    """Best-available parse: Gemini when a key is configured, regex otherwise.

    Both paths return the same shape, tagged with ``source`` so the UI can
    say which parser answered. The LLM path falls back to regex on any
    failure (bad key, network, model refusal) -- parsing must never break.
    """
    from . import llm

    out = llm.parse_trade_note(text) if llm.enabled() else None
    if out is None:
        out = parse_notes(text)
        out["direction"] = "long"
        out["summary"] = None
        out["source"] = "regex" + (" — add a Gemini key for smarter parsing"
                                   if not llm.enabled() else " (Gemini unavailable)")
        return out
    # same sanity ordering the regex parser applies, respecting direction
    if out["direction"] != "short":
        if out["entry_high"] and out["stop"] and out["stop"] >= out["entry_high"]:
            out["warnings"].append("stop is not below the entry zone -- check levels")
        if out["entry_high"] and out["target"] and out["target"] <= out["entry_high"]:
            out["warnings"].append("target is not above the entry zone -- check levels")
    for k in ("ticker", "entry_high", "stop", "target"):
        if not out.get(k):
            out["warnings"].append(f"could not find {k.replace('_', ' ')}")
    return out


# ----------------------------------------------------------------- orders

def _guard() -> str | None:
    """Why trading can't happen right now, or None when it can."""
    if not broker.enabled("tvtester"):
        return "Alpaca keys not configured (alpaca.json)"
    if not broker.channel_active("tvtester"):
        return "TV strategy tester paper trading is PAUSED -- activate it first"
    return None


def arm_plan(ticker: str, qty: int, entry: float, stop: float, target: float,
             notes: str = "") -> dict:
    """Submit the plan as one server-side bracket order on Alpaca.

    Limit buy at `entry` (top of the pullback zone: fills anywhere at or
    below it), stop-loss and take-profit legs attached, GTC. The broker
    then manages the whole trade even when this app is not running.
    """
    err = _guard()
    if err:
        return {"error": err}
    ticker = ticker.upper().strip().replace("-", ".")
    if not (stop < entry < target):
        return {"error": f"levels must satisfy stop < entry < target "
                         f"(got {stop} / {entry} / {target})"}
    if qty < 1:
        return {"error": "qty must be at least 1 (brackets cannot be fractional)"}

    d = _load()
    d["seq"] += 1
    client_id = f"{TAG}p{d['seq']}_{uuid.uuid4().hex[:6]}"
    try:
        order = broker._request("POST", "/v2/orders", broker._config("tvtester"), json={
            "symbol": ticker,
            "qty": str(qty),
            "side": "buy",
            "type": "limit",
            "limit_price": f"{entry:.2f}",
            "time_in_force": "gtc",
            "order_class": "bracket",
            "take_profit": {"limit_price": f"{target:.2f}"},
            "stop_loss": {"stop_price": f"{stop:.2f}"},
            "client_order_id": client_id,
        })
    except Exception as exc:
        resp = getattr(exc, "response", None)
        return {"error": resp.text if resp is not None else str(exc)}

    plan = {
        "id": d["seq"], "ticker": ticker, "qty": qty,
        "entry": entry, "stop": stop, "target": target,
        "notes": (notes or "").strip()[:2000],
        "order_id": order["id"], "client_id": client_id,
        "created": datetime.now(NY).strftime("%Y-%m-%d %H:%M"),
        "status": order["status"],
    }
    d["plans"].insert(0, plan)
    _save(d)
    print(f"[tvtester] plan #{plan['id']} {ticker}: buy {qty} @ {entry} "
          f"stop {stop} target {target} (order {order['id'][:8]})")
    return {"plan": plan}


def manual_order(ticker: str, side: str, qty: int, limit: float | None = None) -> dict:
    """One-off buy/sell from the tab (market, or limit when a price is given)."""
    err = _guard()
    if err:
        return {"error": err}
    ticker = ticker.upper().strip().replace("-", ".")
    if side not in ("buy", "sell"):
        return {"error": "side must be buy or sell"}
    if qty < 1:
        return {"error": "qty must be at least 1"}
    d = _load()
    d["seq"] += 1
    _save(d)  # burn the seq even if the order is rejected, ids stay unique
    payload = {
        "symbol": ticker, "qty": str(qty), "side": side,
        "type": "limit" if limit else "market",
        "time_in_force": "gtc" if limit else "day",
        "client_order_id": f"{TAG}m{d['seq']}_{uuid.uuid4().hex[:6]}",
    }
    if limit:
        payload["limit_price"] = f"{float(limit):.2f}"
    try:
        order = broker._request("POST", "/v2/orders", broker._config("tvtester"), json=payload)
    except Exception as exc:
        resp = getattr(exc, "response", None)
        return {"error": resp.text if resp is not None else str(exc)}
    print(f"[tvtester] manual {side} {qty} {ticker} "
          f"@ {limit or 'market'} (order {order['id'][:8]})")
    return {"order": {"id": order["id"], "status": order["status"],
                      "symbol": ticker, "side": side, "qty": qty}}


def cancel_plan(plan_id: int) -> dict:
    """Cancel a plan's parent order (legs die with it) and mark it."""
    d = _load()
    plan = next((p for p in d["plans"] if p["id"] == plan_id), None)
    if plan is None:
        return {"error": f"no plan #{plan_id}"}
    try:
        broker._request("DELETE", f"/v2/orders/{plan['order_id']}",
                        broker._config("tvtester"))
    except Exception as exc:
        # 422 = already filled/cancelled; reflect reality either way
        print(f"[tvtester] cancel plan #{plan_id}: {exc}")
    plan["status"] = "cancel_requested"
    _save(d)
    return {"plan": plan}


# ---------------------------------------------------------------- reporting

def _tv_orders() -> list[dict]:
    """All Alpaca orders this tab ever sent (parents + bracket legs)."""
    rows = broker._request(
        "GET", "/v2/orders?status=all&limit=500&nested=true&direction=desc",
        broker._config("tvtester"))
    out = []
    for o in rows:
        if not (o.get("client_order_id") or "").startswith(TAG):
            continue
        out.append(o)
        out.extend(o.get("legs") or [])
    return out


def _order_row(o: dict) -> dict:
    return {
        "symbol": o["symbol"],
        "side": o["side"],
        "type": o["type"],
        "qty": float(o.get("qty") or 0),
        "limit_price": float(o["limit_price"]) if o.get("limit_price") else None,
        "stop_price": float(o["stop_price"]) if o.get("stop_price") else None,
        "filled_qty": float(o.get("filled_qty") or 0),
        "filled_avg_price": float(o["filled_avg_price"]) if o.get("filled_avg_price") else None,
        "status": o["status"],
        "submitted_at": (o.get("submitted_at") or "")[:19].replace("T", " "),
        "filled_at": (o.get("filled_at") or "")[:19].replace("T", " ") or None,
        "id": o["id"],
    }


def summary() -> dict:
    """Everything the tab's bottom panel shows.

    trades      -- every order this tab sent, newest first (any status)
    closed      -- per-symbol realized P/L where both a buy and a sell
                   filled: matched qty, avg in/out, $ and % result
    open        -- current paper positions in symbols this tab traded
    plans       -- saved plans with their live order status
    channel     -- active/paused flag for the tab's trade gate
    """
    if not broker.enabled("tvtester"):
        return {"enabled": False, "channels": broker.channels(),
                "trades": [], "closed": [], "open": [], "plans": _load()["plans"]}
    try:
        orders = _tv_orders()
    except Exception as exc:
        return {"enabled": True, "error": str(exc),
                "channels": broker.channels(), "trades": [], "closed": [],
                "open": [], "plans": _load()["plans"]}

    trades = [_order_row(o) for o in orders]

    # ---- realized P/L per symbol: match filled buy qty against sell qty
    per: dict[str, dict] = {}
    for t in trades:
        if t["filled_qty"] <= 0 or t["filled_avg_price"] is None:
            continue
        s = per.setdefault(t["symbol"], {
            "buy_qty": 0.0, "buy_cost": 0.0, "sell_qty": 0.0, "sell_cost": 0.0})
        if t["side"] == "buy":
            s["buy_qty"] += t["filled_qty"]
            s["buy_cost"] += t["filled_qty"] * t["filled_avg_price"]
        else:
            s["sell_qty"] += t["filled_qty"]
            s["sell_cost"] += t["filled_qty"] * t["filled_avg_price"]
    closed = []
    for sym, s in sorted(per.items()):
        matched = min(s["buy_qty"], s["sell_qty"])
        if matched <= 0:
            continue
        avg_in = s["buy_cost"] / s["buy_qty"]
        avg_out = s["sell_cost"] / s["sell_qty"]
        pl = (avg_out - avg_in) * matched
        closed.append({
            "symbol": sym, "closed_qty": matched,
            "avg_in": round(avg_in, 2), "avg_out": round(avg_out, 2),
            "pl": round(pl, 2),
            "pl_pct": round(100 * (avg_out / avg_in - 1), 2) if avg_in else None,
        })

    # ---- open paper positions for symbols this tab touched
    open_rows = []
    try:
        symbols = {t["symbol"] for t in trades}
        for p in broker._request("GET", "/v2/positions", broker._config("tvtester")):
            if p["symbol"] in symbols:
                open_rows.append({
                    "symbol": p["symbol"], "qty": float(p["qty"]),
                    "avg_entry": float(p["avg_entry_price"]),
                    "current": float(p["current_price"]),
                    "unrealized_pl": float(p["unrealized_pl"]),
                    "unrealized_plpc": round(100 * float(p["unrealized_plpc"]), 2),
                })
    except Exception as exc:
        print(f"[tvtester] positions fetch failed: {exc}")

    # ---- refresh saved plans with the live status of their parent order
    d = _load()
    by_id = {o["id"]: o for o in orders}
    dirty = False
    for plan in d["plans"]:
        live = by_id.get(plan["order_id"])
        if live and live["status"] != plan["status"]:
            plan["status"] = live["status"]
            dirty = True
    if dirty:
        _save(d)

    return {"enabled": True, "channels": broker.channels(),
            "trades": trades, "closed": closed, "open": open_rows,
            "plans": d["plans"]}
