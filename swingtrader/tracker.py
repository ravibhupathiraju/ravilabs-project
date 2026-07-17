"""Misc-tab daily tracker: a small persistent watchlist of divergence picks.

Add candidates from the divergence scan, watch price-since-added each day,
then archive (with a note) or delete them. Backed by a JSON file so it
survives restarts; state is tiny (a handful of tickers), so no database.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from . import marketdata
from .data import load_history

NY = ZoneInfo("America/New_York")
PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "misc_tracker.json"
)


def _load() -> dict:
    if not os.path.exists(PATH):
        return {"seq": 0, "active": [], "archived": []}
    try:
        with open(PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        d.setdefault("seq", 0)
        d.setdefault("active", [])
        d.setdefault("archived", [])
        return d
    except Exception as exc:
        print(f"[tracker] could not read {PATH}: {exc}")
        return {"seq": 0, "active": [], "archived": []}


def _save(d: dict) -> None:
    with open(PATH, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=2)


def _price_now(ticker: str) -> float | None:
    """Latest real-time price (Alpaca IEX), falling back to the daily close."""
    p = marketdata.latest_price(ticker)
    if p:
        return round(float(p), 2)
    df = load_history(ticker, days=10)
    if not df.empty:
        return round(float(df["Close"].iloc[-1]), 2)
    return None


def add(items: list[dict]) -> dict:
    """Add scan picks to the active tracker; skips tickers already tracked."""
    d = _load()
    have = {r["ticker"] for r in d["active"]}
    today = datetime.now(NY).strftime("%Y-%m-%d")
    for it in items:
        t = (it.get("ticker") or "").upper().strip()
        if not t or t in have:
            continue
        d["seq"] += 1
        d["active"].append({
            "id": d["seq"], "ticker": t, "added_date": today,
            "added_price": round(float(it.get("price") or 0), 2) or _price_now(t),
            "score_at_add": it.get("score"), "direction": it.get("direction"),
            "kind_at_add": it.get("kind"), "note": "",
        })
        have.add(t)
    _save(d)
    return list_all()


def delete(item_id: int, bucket: str = "active") -> dict:
    d = _load()
    key = "archived" if bucket == "archived" else "active"
    d[key] = [r for r in d[key] if r["id"] != item_id]
    _save(d)
    return list_all()


def archive(item_id: int, note: str = "") -> dict:
    d = _load()
    row = next((r for r in d["active"] if r["id"] == item_id), None)
    if row is None:
        return list_all()
    d["active"] = [r for r in d["active"] if r["id"] != item_id]
    row["archived_date"] = datetime.now(NY).strftime("%Y-%m-%d")
    row["archive_note"] = (note or "").strip()
    row["final_price"] = _price_now(row["ticker"])
    d["archived"].insert(0, row)
    _save(d)
    return list_all()


def _enrich(row: dict) -> dict:
    """Attach the live price and return-since-added to an active row."""
    now = _price_now(row["ticker"])
    out = {**row, "price_now": now}
    ap = row.get("added_price")
    if now and ap:
        out["change_pct"] = round((now / ap - 1) * 100, 2)
        # Signed to the thesis: a short pick profits when price falls.
        out["pnl_pct"] = out["change_pct"] * (-1 if row.get("direction") == "bearish" else 1)
    else:
        out["change_pct"] = out["pnl_pct"] = None
    return out


def list_all() -> dict:
    d = _load()
    with ThreadPoolExecutor(max_workers=8) as pool:
        active = list(pool.map(_enrich, d["active"]))
    active.sort(key=lambda r: (r["added_date"], r["ticker"]))
    return {"active": active, "archived": d["archived"]}
