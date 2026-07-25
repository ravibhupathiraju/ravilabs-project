"""Unified strategy performance across the per-strategy paper accounts.

One consistent way to answer "how is each strategy doing?" for all three
trading channels (Swing, 0DTE, TV tester). Each channel trades on its own
Alpaca paper account, so its fill history IS its strategy record: this
module pulls the filled orders from each account, matches them into round
trips (FIFO, long and short aware), and aggregates.

Attribution comes from the client-order-id tags every entry carries
(``sw_<strategy>_``, ``zd_<strategy>_``, ``tvst_p``/``tvst_m``), parsed
back out of the broker's own history -- so the numbers survive app
restarts, database loss, anything local. Untagged orders (manual dashboard
trades, pre-tagging history) show up under strategy "untagged" rather than
being hidden.

Filters: channels, date range (on exit time), symbols, strategy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import broker

NY = ZoneInfo("America/New_York")


def _to_et(iso: str) -> str:
    """Alpaca fill timestamps are UTC (…Z). Convert to Eastern wall-clock so
    times read as market hours (a 15:55 ET flatten showed as 19:55 raw), and
    so the date used for filtering is the actual ET trading day. Returns
    'YYYY-MM-DDTHH:MM:SS' (or the raw first 19 chars if it can't be parsed)."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(NY).isoformat()[:19]
    except Exception:
        return iso[:19]
CHANNELS = ("swing", "zerodte", "tvtester", "pattern")
CHANNEL_LABEL = {"swing": "Swing", "zerodte": "0DTE", "tvtester": "TV tester",
                 "pattern": "Pattern"}


def _strategy_of(client_id: str) -> str:
    """Parse the strategy back out of our client-order-id tags."""
    cid = client_id or ""
    if cid.startswith("tvst_g"):
        return "gemini-auto"   # morning Gemini auto-picker
    if cid.startswith("tvst_p"):
        return "tv-plan"       # manually pasted TradingView-copilot plan
    if cid.startswith("tvst_m"):
        return "manual"
    if cid.startswith("pt_"):
        return "pattern"       # Pattern-lab live match (on the 0DTE account)
    if cid.startswith(("sw_", "zd_")):
        parts = cid.split("_")
        if len(parts) >= 3 and parts[1] and parts[1] != "x":
            return parts[1]
    return "untagged"


def _label(channel: str, strategy: str) -> str:
    """Display bucket. Pattern trades share the 0DTE account but report under
    their own 'Pattern' bucket so the two strategies never blur together."""
    if strategy == "pattern":
        return "Pattern"
    return CHANNEL_LABEL.get(channel, channel)


def _filled_orders(channel: str, start: str | None, end: str | None) -> list[dict]:
    """Every filled order (parents + bracket legs) on the channel's account.

    Pages backwards through /v2/orders until the window is covered (capped
    to stay snappy; 2500 orders is months of this app's volume).
    """
    cfg = broker._config(channel)
    if not (cfg["key_id"] and cfg["secret_key"]):
        return []
    after = f"{start}T00:00:00-05:00" if start else None
    until = f"{end}T23:59:59-05:00" if end else None
    rows: list[dict] = []
    for _page in range(5):
        path = "/v2/orders?status=closed&limit=500&nested=true&direction=desc"
        if after:
            path += f"&after={after}"
        if until:
            path += f"&until={until}"
        batch = broker._request("GET", path, cfg)
        rows.extend(batch)
        if len(batch) < 500:
            break
        until = batch[-1]["submitted_at"]  # next page: strictly older

    fills: list[dict] = []
    seen: set[str] = set()
    stack = list(rows)
    while stack:
        o = stack.pop()
        if o["id"] in seen:
            continue
        seen.add(o["id"])
        stack.extend(o.get("legs") or [])
        if float(o.get("filled_qty") or 0) <= 0 or not o.get("filled_avg_price"):
            continue
        fills.append({
            "symbol": o["symbol"],
            "side": o["side"],
            "qty": float(o["filled_qty"]),
            "price": float(o["filled_avg_price"]),
            "time": _to_et(o.get("filled_at") or o.get("submitted_at") or ""),
            "strategy": _strategy_of(o.get("client_order_id")),
            "order_type": o["type"],
        })
    fills.sort(key=lambda f: f["time"])
    return fills


def _round_trips(fills: list[dict], channel: str) -> tuple[list[dict], list[dict]]:
    """FIFO-match fills into closed round trips; long and short aware.

    Returns (trades, open_lots). A buy against a short inventory closes the
    short; a sell against long inventory closes the long. The entry fill's
    strategy tag names the trade (bracket exit legs carry no tag).
    """
    trades: list[dict] = []
    open_lots: list[dict] = []
    by_symbol: dict[str, list[dict]] = {}
    for f in fills:
        by_symbol.setdefault(f["symbol"], []).append(f)

    for symbol, sf in by_symbol.items():
        lots: list[dict] = []  # signed qty: + long, - short
        for f in sf:
            q = f["qty"] if f["side"] == "buy" else -f["qty"]
            while abs(q) > 1e-9 and lots and lots[0]["qty"] * q < 0:
                lot = lots[0]
                m = min(abs(q), abs(lot["qty"]))
                direction = "long" if lot["qty"] > 0 else "short"
                entry_px, exit_px = lot["price"], f["price"]
                pl = (exit_px - entry_px) * m if direction == "long" \
                    else (entry_px - exit_px) * m
                base = entry_px * m
                trades.append({
                    "channel": channel,
                    "channel_label": _label(channel, lot["strategy"]),
                    "symbol": symbol,
                    "strategy": lot["strategy"],
                    "direction": direction,
                    "qty": m,
                    "entry_time": lot["time"],
                    "exit_time": f["time"],
                    "entry_price": round(entry_px, 4),
                    "exit_price": round(exit_px, 4),
                    "pl": round(pl, 2),
                    "pl_pct": round(100 * pl / base, 2) if base else None,
                })
                lot["qty"] += m if lot["qty"] < 0 else -m
                q += m if q < 0 else -m
                if abs(lot["qty"]) < 1e-9:
                    lots.pop(0)
            if abs(q) > 1e-9:
                lots.append({"qty": q, "price": f["price"], "time": f["time"],
                             "strategy": f["strategy"]})
        for lot in lots:
            open_lots.append({
                "channel": channel,
                "channel_label": _label(channel, lot["strategy"]),
                "symbol": symbol,
                "direction": "long" if lot["qty"] > 0 else "short",
                "qty": abs(lot["qty"]), "entry_price": round(lot["price"], 4),
                "entry_time": lot["time"], "strategy": lot["strategy"],
            })
    return trades, open_lots


def _agg(trades: list[dict], key) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for t in trades:
        groups.setdefault(key(t), []).append(t)
    out = []
    for name, ts in groups.items():
        wins = [t for t in ts if t["pl"] > 0]
        out.append({
            "name": name,
            "trades": len(ts),
            "wins": len(wins),
            "win_rate": round(100 * len(wins) / len(ts), 1),
            "total_pl": round(sum(t["pl"] for t in ts), 2),
            "avg_pl": round(sum(t["pl"] for t in ts) / len(ts), 2),
            "avg_pl_pct": round(sum(t["pl_pct"] or 0 for t in ts) / len(ts), 2),
        })
    out.sort(key=lambda r: r["total_pl"], reverse=True)
    return out


def _live_positions(channel: str) -> dict[str, dict]:
    """Current price + live unrealized P/L per symbol, straight from Alpaca
    (the account's own mark, not a second price fetch)."""
    cfg = broker._config(channel)
    if not (cfg["key_id"] and cfg["secret_key"]):
        return {}
    try:
        rows = broker._request("GET", "/v2/positions", cfg)
    except Exception:
        return {}
    return {p["symbol"]: {
        "current": float(p["current_price"]),
        "unrealized_pl": float(p["unrealized_pl"]),
        "unrealized_plpc": round(100 * float(p["unrealized_plpc"]), 2),
    } for p in rows}


def _mark_open_lots(open_lots: list[dict], chans: list[str]) -> None:
    """Attach live price + unrealized P/L to each open lot in place."""
    live_by_chan = {ch: _live_positions(ch) for ch in chans}
    for o in open_lots:
        live = live_by_chan.get(o["channel"], {}).get(o["symbol"])
        if live:
            o["current"] = live["current"]
            o["unrealized_pl"] = round(live["unrealized_pl"], 2)
            o["unrealized_pl_pct"] = live["unrealized_plpc"]
        else:
            o["current"] = o["unrealized_pl"] = o["unrealized_pl_pct"] = None


def _combined(closed_rows: list[dict], open_lots: list[dict], key) -> list[dict]:
    """Merge realized (closed) with unrealized (open) totals under one name
    per group -- e.g. one PG row shows its realized P/L AND its still-open
    unrealized P/L side by side, so nothing needs mental addition."""
    names = {r["name"] for r in closed_rows} | {key(o) for o in open_lots}
    by_closed = {r["name"]: r for r in closed_rows}
    open_pl: dict[str, float] = {}
    open_ct: dict[str, int] = {}
    for o in open_lots:
        n = key(o)
        open_pl[n] = open_pl.get(n, 0) + (o["unrealized_pl"] or 0)
        open_ct[n] = open_ct.get(n, 0) + 1
    out = []
    for name in names:
        c = by_closed.get(name)
        realized = c["total_pl"] if c else 0.0
        unrealized = round(open_pl.get(name, 0.0), 2)
        out.append({
            "name": name,
            "closed_trades": c["trades"] if c else 0,
            "win_rate": c["win_rate"] if c else None,
            "realized_pl": realized,
            "open_positions": open_ct.get(name, 0),
            "unrealized_pl": unrealized,
            "combined_pl": round(realized + unrealized, 2),
        })
    out.sort(key=lambda r: r["combined_pl"], reverse=True)
    return out


def analyze(channels: list[str] | None = None,
            start: str | None = None, end: str | None = None,
            symbols: list[str] | None = None,
            strategy: str | None = None) -> dict:
    """The one performance view: closed round trips + aggregates, filtered.

    channels -- subset of (swing, zerodte, tvtester); default all
    start/end -- YYYY-MM-DD, applied to the exit time (default last 90 days)
    symbols  -- restrict to these tickers
    strategy -- restrict to one strategy tag (e.g. 'vwap', 'plan')
    """
    chans = [c for c in (channels or CHANNELS) if c in CHANNELS] or list(CHANNELS)
    if not start:
        start = (datetime.now(NY) - timedelta(days=90)).strftime("%Y-%m-%d")
    want_syms = {s.upper().strip().replace("-", ".") for s in (symbols or []) if s.strip()}

    trades: list[dict] = []
    open_lots: list[dict] = []
    errors: dict[str, str] = {}
    for ch in chans:
        try:
            t, o = _round_trips(_filled_orders(ch, start, end), ch)
            trades += t
            open_lots += o
        except Exception as exc:
            errors[ch] = str(exc)

    if want_syms:
        trades = [t for t in trades if t["symbol"] in want_syms]
        open_lots = [o for o in open_lots if o["symbol"] in want_syms]
    if end:
        trades = [t for t in trades if t["exit_time"][:10] <= end]
    trades = [t for t in trades if t["exit_time"][:10] >= start]
    # strategy list BEFORE the strategy filter, so the UI dropdown stays full
    strategies = sorted({t["strategy"] for t in trades})
    if strategy:
        trades = [t for t in trades if t["strategy"] == strategy]

    trades.sort(key=lambda t: t["exit_time"], reverse=True)
    wins = [t for t in trades if t["pl"] > 0]
    total = sum(t["pl"] for t in trades)
    summary = {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(trades) - len(wins),
        "win_rate": round(100 * len(wins) / len(trades), 1) if trades else None,
        "total_pl": round(total, 2),
        "avg_pl": round(total / len(trades), 2) if trades else None,
        "best": max((t["pl"] for t in trades), default=None),
        "worst": min((t["pl"] for t in trades), default=None),
    }

    # live mark on open lots so unrealized P/L is visible without a side trip
    # to the broker boxes -- filtered to the requested symbols like closed
    _mark_open_lots(open_lots, chans)
    open_view = [o for o in open_lots if not want_syms or o["symbol"] in want_syms]
    unrealized_total = round(sum(o["unrealized_pl"] or 0 for o in open_view), 2)
    summary["unrealized_pl"] = unrealized_total
    summary["combined_pl"] = round(summary["total_pl"] + unrealized_total, 2)
    summary["open_positions"] = len(open_view)

    by_channel = _agg(trades, lambda t: t["channel_label"])
    by_strategy = _agg(trades, lambda t: f"{t['channel_label']} · {t['strategy']}")
    by_symbol = _agg(trades, lambda t: t["symbol"])
    return {
        "summary": summary,
        "by_channel": by_channel,
        "by_strategy": by_strategy,
        "by_symbol": by_symbol,
        "combined_by_channel": _combined(by_channel, open_view, lambda o: o["channel_label"]),
        "combined_by_strategy": _combined(by_strategy, open_view,
            lambda o: f"{o['channel_label']} · {o['strategy']}"),
        "combined_by_symbol": _combined(by_symbol, open_view, lambda o: o["symbol"]),
        "trades": trades[:400],
        "open_lots": open_view,
        "strategies": strategies,
        "channels_analyzed": chans,
        "start": start,
        "end": end,
        "errors": errors,
    }
