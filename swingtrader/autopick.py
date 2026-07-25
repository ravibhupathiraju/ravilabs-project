"""Morning Gemini auto-picker: scan -> shortlist -> pick top 2 -> arm.

Runs every weekday morning before the open (6:15 AM PST / 9:15 ET, scheduled
by the alert monitor). The pipeline is deliberately grounded in OUR data so
Gemini selects among real setups instead of inventing levels:

  1. fibdiv.scan over the watchlist universe -> compact technical rows
     (score -100..100, divergence, trend, golden pocket, targets).
  2. Shortlist the strongest bullish candidates (brackets are long-only).
  3. Gemini gets the shortlist as JSON and picks AT MOST 2 "A+" setups it
     has very high confidence in -- with entry/stop/target that must come
     from the levels provided. It is told to return [] when nothing
     qualifies: no forced trades on a bad morning.
  4. Guardrails re-validate every pick (levels sane, risk bounded, ticker
     not already armed/held), then arm_plan(source="gemini") submits the
     bracket -- tagged tvst_g so performance analysis can score the Gemini
     picks against your manually pasted TradingView plans (tvst_p).

Results land in app_settings.json under "autopick_last" for the UI.
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from . import broker, fibdiv, llm, tvtester
from .universe import get_universe

NY = ZoneInfo("America/New_York")

MAX_PICKS = 2
SHORTLIST = 8          # candidates offered to Gemini
NOTIONAL = 5_000.0     # $ per auto-armed plan (whole shares)
MAX_STOP_PCT = 10.0    # reject picks risking more than this to the stop
MIN_STOP_PCT = 1.0     # ...or less: sub-1% swing stops get shaken out by noise
MIN_RR = 1.0           # target must pay at least 1x the risk

PROMPT = """You are a conservative swing-trade selector. Below are today's
technical snapshots for candidate stocks, produced by a real scanner:
score is -100 (strong sell) .. +100 (strong buy); gp is the Fibonacci
golden-pocket support zone [low, high]; targets are measured-move levels
above; zone/pct_of_swing say where price sits in its swing.

Pick AT MOST {n} LONG setups you have VERY HIGH confidence in ("A+" only).
Prefer: confirmed bullish divergence, uptrend, price pulling back into or
holding the golden pocket, MACD agreeing. If nothing truly qualifies,
return fewer picks or an empty list -- do NOT force trades.

For each pick derive levels FROM THE DATA GIVEN (never invent numbers):
  entry  - pullback limit-buy at/below the current price (e.g. top of the
           golden pocket, or a modest pullback), entry < price
  stop   - just below the golden pocket low or recent structure; must sit
           1%-10% below the entry (closer is noise, wider is reckless)
  target - the first measured-move target
Return ONLY a JSON array (possibly empty) of objects:
  {{"ticker": str, "entry": number, "stop": number, "target": number,
    "confidence": 0-100, "reason": one short sentence}}

CANDIDATES:
"""


def _shortlist() -> list[dict]:
    tickers = get_universe("watchlist")
    rows = fibdiv.scan(tickers, only_divergence=False)
    # long-only: bullish lean, reasonable score, must have GP + a target
    cands = [r for r in rows
             if r["score"] >= 20 and r.get("gp") and r.get("targets")
             and r.get("direction") != "bearish"]
    return cands[:SHORTLIST]


def _validate(p: dict, by_ticker: dict) -> str | None:
    """Guardrails on one Gemini pick. Returns a rejection reason or None."""
    t = p.get("ticker")
    if t not in by_ticker:
        return f"{t}: not in the shortlist"
    row = by_ticker[t]
    entry, stop, target = p.get("entry"), p.get("stop"), p.get("target")
    if not all(isinstance(x, (int, float)) for x in (entry, stop, target)):
        return f"{t}: non-numeric levels"
    if not (stop < entry < target):
        return f"{t}: levels out of order ({stop}/{entry}/{target})"
    if entry > row["price"] * 1.001:
        return f"{t}: entry {entry} above current price {row['price']}"
    risk_pct = 100 * (entry - stop) / entry
    if risk_pct > MAX_STOP_PCT:
        return f"{t}: stop {risk_pct:.1f}% away (max {MAX_STOP_PCT}%)"
    if risk_pct < MIN_STOP_PCT:
        return f"{t}: stop only {risk_pct:.1f}% away (noise; min {MIN_STOP_PCT}%)"
    if (target - entry) < MIN_RR * (entry - stop):
        return f"{t}: reward/risk below {MIN_RR}"
    return None


def run(dry_run: bool = False) -> dict:
    """One morning cycle. Returns what happened, for the log and the UI."""
    started = datetime.now(NY).strftime("%Y-%m-%d %H:%M")
    out = {"ran_at": started, "picks": [], "armed": [], "rejected": [],
           "dry_run": dry_run}

    if not llm.enabled():
        out["error"] = "Gemini key not configured"
        return out
    if not (broker.enabled("tvtester") and broker.channel_active("tvtester")):
        out["error"] = "tvtester channel disabled or not configured"
        return out

    cands = _shortlist()
    out["candidates"] = [c["ticker"] for c in cands]
    if not cands:
        out["note"] = "no bullish candidates this morning"
        return out

    slim = [{k: c[k] for k in ("ticker", "score", "label", "direction", "kind",
                               "price", "zone", "pct_of_swing", "trend",
                               "macd_bullish", "confirmed", "gp", "targets")}
            for c in cands]
    picks = llm.ask_json(PROMPT.format(n=MAX_PICKS) + json.dumps(slim))
    if not isinstance(picks, list):
        out["error"] = "Gemini returned no usable answer"
        return out
    picks = sorted(
        [p for p in picks if isinstance(p, dict)],
        key=lambda p: -(p.get("confidence") or 0))[:MAX_PICKS]
    out["picks"] = picks
    if not picks:
        out["note"] = "Gemini found no A+ setup today"
        return out

    by_ticker = {c["ticker"]: c for c in cands}
    already = {p["ticker"] for p in tvtester._load()["plans"]
               if p["status"] in ("new", "accepted", "held", "partially_filled",
                                  "pending_new", "filled")}
    for p in picks:
        why = _validate(p, by_ticker)
        if why is None and p["ticker"] in already:
            why = f"{p['ticker']}: already has an active plan"
        if why:
            out["rejected"].append(why)
            continue
        qty = int(NOTIONAL / p["entry"])
        if qty < 1:
            out["rejected"].append(f"{p['ticker']}: price above ${NOTIONAL:,.0f} notional")
            continue
        note = (f"[gemini-auto {started}] confidence {p.get('confidence')}%: "
                f"{p.get('reason', '')}")
        if dry_run:
            out["armed"].append({"ticker": p["ticker"], "qty": qty,
                                 "entry": p["entry"], "stop": p["stop"],
                                 "target": p["target"], "dry_run": True})
            continue
        res = tvtester.arm_plan(p["ticker"], qty, float(p["entry"]),
                                float(p["stop"]), float(p["target"]),
                                notes=note, source="gemini")
        if "plan" in res:
            out["armed"].append(res["plan"])
        else:
            out["rejected"].append(f"{p['ticker']}: {res.get('error')}")

    print(f"[autopick] {started}: {len(out['armed'])} armed, "
          f"{len(out['rejected'])} rejected "
          f"({'; '.join(out['rejected']) or 'none'})")
    return out
