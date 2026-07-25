"""Deep-dive intraday pattern mining ("Pattern lab").

Goal: find combinations of measurable, known-at-the-time market conditions
that consistently precede the price touching +$target before -$target (or
the reverse) from the current price, any time before the same session's
close -- the shape of a same-day open/close trade. Built for QQQ but works
for any liquid symbol.

Method (first-touch barrier study):
  * Decision points every `interval_min` minutes, 10:00-14:30 ET. The
    decision price P is the close of the 5-minute bar starting there.
  * Outcome, scanned over the REST of the session only: "up" if the price
    touches P+target before P-target, "dn" for the reverse, "none" if
    neither is touched by the close, "ambig" when both barriers fall
    inside the same 5-minute bar (counted as a win for NEITHER side --
    conservative).
  * Each decision point is described by 12 discretized features, all
    computable at that moment (no look-ahead): time of day, overnight gap,
    opening-range position, VWAP side and slope, intraday RSI(14), position
    in the prior day's range, day of week, first-30-minute direction,
    prior-day direction, share of the 14-day average range already used,
    and 30-minute momentum.
  * Mining: every single feature, pair and triple of features is grouped
    over its observed bucket combinations. Days are split ~70/30
    (older/newer) and a combo is scored by its WORST half --
    min(train rate, test rate) -- and only kept when both halves beat the
    baseline, so only conditions that also kept working recently surface.
    Supersets that don't beat the simpler pattern they contain are dropped.

Scheduled event days are excluded: FOMC decision days (static list below,
2023-2026), NFP Fridays (first Friday, computed), half sessions (computed),
plus any dates listed in event_days.json ("cpi", "custom" -- paste dates
there to widen the net; counts of everything excluded are reported).

Honest caveats (also surfaced in the UI): decision points within one day
overlap and are correlated -- judge patterns by distinct days, not just
samples; and with ~300 combos tested, some will look good by luck. The
train/test consistency requirement reduces that, it cannot eliminate it.
"""

from __future__ import annotations

import itertools
import json
import os
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NY = ZoneInfo("America/New_York")

from . import marketdata
from .data import load_history

# ------------------------------------------------------------ event days

# FOMC decision days (second day of each scheduled meeting).
FOMC = [
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14",
    "2023-07-26", "2023-09-20", "2023-11-01", "2023-12-13",
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12",
    "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]


def _first_fridays(years) -> list[str]:
    """NFP release days: the first Friday of every month."""
    out = []
    for y in years:
        for m in range(1, 13):
            d = date(y, m, 1)
            d += timedelta(days=(4 - d.weekday()) % 7)
            out.append(d.isoformat())
    return out


def _half_days(years) -> list[str]:
    """Early closes: July 3, the day after Thanksgiving, Christmas Eve."""
    out = []
    for y in years:
        d = date(y, 7, 3)
        if d.weekday() < 5:
            out.append(d.isoformat())
        thu = date(y, 11, 1)
        thu += timedelta(days=(3 - thu.weekday()) % 7)  # first Thursday
        out.append((thu + timedelta(days=22)).isoformat())  # 4th Thu + 1
        d = date(y, 12, 24)
        if d.weekday() < 5:
            out.append(d.isoformat())
    return out


def _event_map(years) -> dict[str, str]:
    """date -> exclusion reason. event_days.json extends the built-ins."""
    file_ev = {}
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "event_days.json"
    )
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                file_ev = json.load(fh)
        except Exception as exc:
            print(f"[patterns] could not read event_days.json: {exc}")
    out: dict[str, str] = {}
    # Later entries win, so the priority order here is lowest-first.
    for reason, dates in (
        ("custom", file_ev.get("custom") or []),
        ("half_day", _half_days(years)),
        ("nfp", _first_fridays(years)),
        ("cpi", file_ev.get("cpi") or []),
        ("fomc", FOMC + list(file_ev.get("fomc") or [])),
    ):
        for ds in dates:
            out[str(ds)] = reason
    return out


# --------------------------------------------------------------- dataset

FEATURES = [
    "tod", "gap", "or30", "vwap_side", "vwap_slope", "rsi",
    "pd_pos", "dow", "first30", "yday", "range_used", "mom30",
]


def _decision_times(interval_min: int) -> set[str]:
    out, m = [], 10 * 60
    while m <= 14 * 60 + 30:
        out.append(f"{m // 60:02d}:{m % 60:02d}")
        m += interval_min
    return set(out)


def _bucket(v: float, cuts: list[float], labels: list[str]) -> str:
    for c, lab in zip(cuts, labels):
        if v < c:
            return lab
    return labels[-1]


def _tri(v: float, th: float) -> str:
    return "up" if v > th else "dn" if v < -th else "flat"


def _bucketize(s: dict) -> dict:
    """Map raw scalars at one moment to the 12 discrete feature buckets.

    Shared by the historical study (_build) and the live snapshot
    (live_features) so a pattern is discretized identically in both places.
    """
    P, mid = s["P"], (s["pdh"] + s["pdl"]) / 2
    return {
        "tod": s["tod"],
        "gap": _bucket(s["gap_pct"], [-0.3, -0.1, 0.1, 0.3],
                       ["dn_big", "dn", "flat", "up", "up_big"]),
        "or30": "above_or" if P > s["or_hi"] else "below_or" if P < s["or_lo"] else "inside_or",
        "vwap_side": _bucket(s["dv"], [-0.15, 0, 0.15],
                             ["well_below", "below", "above", "well_above"]),
        "vwap_slope": _tri(s["vslope"], 0.02),
        "rsi": _bucket(s["rsi"], [30, 45, 55, 70],
                       ["<30", "30-45", "45-55", "55-70", ">70"]),
        "pd_pos": ("above_pdh" if P > s["pdh"] else "below_pdl" if P < s["pdl"]
                   else "upper_half" if P >= mid else "lower_half"),
        "dow": s["dow"],
        "first30": _tri(s["f30"], 0.1),
        "yday": _tri(s["yret"], 0.1),
        "range_used": "low" if s["ru"] < 0.5 else "mid" if s["ru"] <= 1.0 else "high",
        "mom30": _tri(s["mom30"], 0.1),
    }


def _rnd(v, k: int = 1):
    return None if v is None or pd.isna(v) else round(float(v), k)


_CACHE: dict = {}


def _build(ticker: str, lookback_days: int, target: float, interval_min: int):
    """Label every decision point in the lookback window; cached per day."""
    key = (ticker, lookback_days, round(target, 2), interval_min, date.today().isoformat())
    if key in _CACHE:
        return _CACHE[key]

    intr = marketdata.bars(ticker, "5Min", days=lookback_days)
    if intr.empty:
        raise ValueError(f"no intraday data for {ticker}")
    daily = load_history(ticker, days=lookback_days + 90)
    if daily.empty:
        raise ValueError(f"no daily data for {ticker}")

    # Daily context, strictly prior-day (shifted) so nothing leaks.
    pdh = daily["High"].shift(1)
    pdl = daily["Low"].shift(1)
    pdc = daily["Close"].shift(1)
    yret = (daily["Close"].shift(1) / daily["Close"].shift(2) - 1) * 100
    adr = (daily["High"] - daily["Low"]).rolling(14).mean().shift(1)
    ctx = {
        ts.date().isoformat(): {"pdh": h, "pdl": l, "pdc": c, "yret": r, "adr": a}
        for ts, h, l, c, r, a in zip(daily.index, pdh, pdl, pdc, yret, adr)
    }

    # Intraday RSI(14), Wilder, continuous across sessions (standard).
    delta = intr["Close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    intr = intr.copy()
    intr["rsi"] = (100 - 100 / (1 + gain / loss.replace(0, np.nan))).fillna(50)

    years = range(intr.index[0].year, intr.index[-1].year + 1)
    events = _event_map(years)
    decisions = _decision_times(interval_min)

    rows: list[dict] = []
    excl: Counter = Counter()
    days_used = 0
    for day, g in intr.groupby(intr.index.date):
        ds = day.isoformat()
        reason = events.get(ds)
        if reason:
            excl[reason] += 1
            continue
        c = ctx.get(ds)
        if c is None or pd.isna(c["adr"]) or pd.isna(c["pdc"]):
            excl["no_daily_context"] += 1
            continue
        hm = np.asarray(g.index.strftime("%H:%M"))
        pre = g.iloc[np.asarray(hm < "10:00", dtype=bool)]
        if len(g) < 40 or len(pre) < 5 or hm[0] != "09:30":
            excl["thin_session"] += 1
            continue

        o = float(g["Open"].iloc[0])
        gap_pct = (o / c["pdc"] - 1) * 100
        or_hi, or_lo = float(pre["High"].max()), float(pre["Low"].min())
        f30 = (float(pre["Close"].iloc[-1]) / o - 1) * 100
        tp = (g["High"] + g["Low"] + g["Close"]) / 3
        vol = g["Volume"].clip(lower=1)  # IEX weights; guard zero-volume bars
        vwap = ((tp * vol).cumsum() / vol.cumsum()).to_numpy()
        highs, lows = g["High"].to_numpy(), g["Low"].to_numpy()
        closes, rsis = g["Close"].to_numpy(), g["rsi"].to_numpy()
        hi_run = np.maximum.accumulate(highs)
        lo_run = np.minimum.accumulate(lows)
        mid = (c["pdh"] + c["pdl"]) / 2

        used = False
        for i in np.flatnonzero(np.isin(hm, list(decisions))):
            if i + 1 >= len(g):
                continue
            P = closes[i]
            up_m = highs[i + 1:] >= P + target
            dn_m = lows[i + 1:] <= P - target
            ju = int(np.argmax(up_m)) if up_m.any() else -1
            jd = int(np.argmax(dn_m)) if dn_m.any() else -1
            if ju < 0 and jd < 0:
                outcome, mins = "none", None
            else:
                if jd < 0 or (0 <= ju < jd):
                    outcome, j = "up", ju
                elif ju < 0 or jd < ju:
                    outcome, j = "dn", jd
                else:
                    outcome, j = "ambig", ju
                mins = (g.index[i + 1 + j] - g.index[i]).total_seconds() / 60
            i6 = max(0, i - 6)
            feats = _bucketize({
                "tod": hm[i], "P": P, "gap_pct": gap_pct,
                "or_hi": or_hi, "or_lo": or_lo,
                "dv": (P - vwap[i]) / vwap[i] * 100,
                "vslope": (vwap[i] - vwap[i6]) / P * 100,
                "rsi": rsis[i], "pdh": c["pdh"], "pdl": c["pdl"],
                "dow": day.strftime("%a"), "f30": f30, "yret": c["yret"],
                "ru": (hi_run[i] - lo_run[i]) / c["adr"],
                "mom30": (P - closes[i6]) / closes[i6] * 100,
            })
            rows.append({**feats, "date": ds, "outcome": outcome, "mins": mins})
            used = True
        if used:
            days_used += 1

    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError("no usable sessions after exclusions")
    meta = {
        "ticker": ticker,
        "target": round(target, 2),
        "interval_min": interval_min,
        "lookback_days": lookback_days,
        "source": intr.attrs.get("source", "?"),
        "date_range": [df["date"].min(), df["date"].max()],
        "days_used": days_used,
        "rows": len(df),
        "excluded": dict(excl),
    }
    _CACHE.clear()  # keep exactly one dataset in memory
    _CACHE[key] = (df, meta)
    return df, meta


# ---------------------------------------------------------------- mining

def _dedupe(items: list[dict], top: int) -> list[dict]:
    """Drop a combo when a simpler pattern it contains scores about as well."""
    kept: list[dict] = []
    for it in sorted(items, key=lambda x: -x["_score"]):
        cset = set(it["conds"].split(" · "))
        dominated = any(
            set(k["conds"].split(" · ")) < cset and k["_score"] >= it["_score"] - 2.0
            for k in kept
        )
        if not dominated:
            kept.append(it)
        if len(kept) == top:
            break
    for k in kept:
        del k["_score"]
    return kept


def _mine(df: pd.DataFrame, min_support: int, top: int = 20) -> dict:
    days_sorted = sorted(df["date"].unique())
    if len(days_sorted) < 10:
        raise ValueError(f"only {len(days_sorted)} usable sessions -- widen the lookback")
    split = days_sorted[int(len(days_sorted) * 0.7)]
    df["is_train"] = df["date"] < split
    df["is_up"] = df["outcome"] == "up"
    df["is_dn"] = df["outcome"] == "dn"
    df["is_none"] = df["outcome"] == "none"
    df["up_tr"] = df["is_up"] & df["is_train"]
    df["dn_tr"] = df["is_dn"] & df["is_train"]
    df["mins_up"] = df["mins"].where(df["is_up"])
    df["mins_dn"] = df["mins"].where(df["is_dn"])

    base_up, base_dn = df["is_up"].mean(), df["is_dn"].mean()
    min_test = max(8, min_support // 5)

    marginals, longs, shorts = [], [], []
    combos = ([(f,) for f in FEATURES]
              + list(itertools.combinations(FEATURES, 2))
              + list(itertools.combinations(FEATURES, 3)))
    for cols in combos:
        agg = df.groupby(list(cols), observed=True).agg(
            n=("is_up", "size"), up=("is_up", "sum"), dn=("is_dn", "sum"),
            none=("is_none", "sum"), days=("date", "nunique"),
            tr_n=("is_train", "sum"), up_tr=("up_tr", "sum"), dn_tr=("dn_tr", "sum"),
            mu=("mins_up", "mean"), md=("mins_dn", "mean"),
        )
        for bkt, r in agg.iterrows():
            bkt = bkt if isinstance(bkt, tuple) else (bkt,)
            conds = " · ".join(f"{c}={v}" for c, v in zip(cols, bkt))
            if len(cols) == 1 and r["n"] >= 20:
                marginals.append({
                    "cond": conds, "n": int(r["n"]), "days": int(r["days"]),
                    "up_pct": _rnd(r["up"] / r["n"] * 100),
                    "dn_pct": _rnd(r["dn"] / r["n"] * 100),
                    "none_pct": _rnd(r["none"] / r["n"] * 100),
                })
            te_n = r["n"] - r["tr_n"]
            if r["n"] < min_support or r["tr_n"] < min_test or te_n < min_test:
                continue
            for wins, wins_tr, base, mins, out in (
                (r["up"], r["up_tr"], base_up, r["mu"], longs),
                (r["dn"], r["dn_tr"], base_dn, r["md"], shorts),
            ):
                tr = wins_tr / r["tr_n"]
                te = (wins - wins_tr) / te_n
                score = min(tr, te)
                if score <= base:  # must beat baseline in BOTH halves
                    continue
                out.append({
                    "conds": conds, "n": int(r["n"]), "days": int(r["days"]),
                    "pct": _rnd(wins / r["n"] * 100),
                    "train_pct": _rnd(tr * 100), "test_pct": _rnd(te * 100),
                    "lift": _rnd((wins / r["n"] - base) * 100),
                    "avg_mins": _rnd(mins, 0),
                    "_score": score * 100,
                })

    return {
        "baseline": {
            "up_pct": _rnd(base_up * 100), "dn_pct": _rnd(base_dn * 100),
            "none_pct": _rnd(df["is_none"].mean() * 100),
            "ambig_pct": _rnd((df["outcome"] == "ambig").mean() * 100),
        },
        "split_date": split,
        "train_days": int(sum(1 for d in days_sorted if d < split)),
        "test_days": int(sum(1 for d in days_sorted if d >= split)),
        "marginals": marginals,
        "long_patterns": _dedupe(longs, top),
        "short_patterns": _dedupe(shorts, top),
    }


# --------------------------------------------------- persistent study cache

CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "patterns_cache.json"
)
CACHE_TTL_DAYS = 30   # a deep dive is reused for a month before recomputing
CACHE_EVICT_DAYS = 120  # forget studies older than this on save


def _validate(target: float, lookback_days: int, interval_min: int, min_support: int) -> None:
    if not 0.1 <= target <= 50:
        raise ValueError("target must be between $0.10 and $50")
    if not 60 <= lookback_days <= 1500:
        raise ValueError("lookback must be 60-1500 days")
    if interval_min not in (15, 30, 60):
        raise ValueError("interval must be 15, 30 or 60 minutes")
    if min_support < 15:
        raise ValueError("min samples must be at least 15")


def _cache_key(ticker: str, lookback_days: int, target: float,
               interval_min: int, min_support: int) -> str:
    return (f"{ticker.upper().strip()}|{round(float(target), 2)}|"
            f"{int(lookback_days)}|{int(interval_min)}|{int(min_support)}")


def _cache_load() -> dict:
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        print(f"[patterns] could not read {CACHE_PATH}: {exc}")
        return {}


def _days_since(iso: str) -> int:
    try:
        return (date.today() - date.fromisoformat(iso)).days
    except Exception:
        return 10 ** 6


def _cache_put(key: str, params: dict, result: dict) -> str:
    store = _cache_load()
    today = date.today().isoformat()
    store[key] = {"computed_at": today, "params": params, "result": result}
    store = {k: v for k, v in store.items()
             if _days_since(v.get("computed_at", "")) <= CACHE_EVICT_DAYS}
    with open(CACHE_PATH, "w", encoding="utf-8") as fh:
        json.dump(store, fh)
    return today


def _run_fresh(ticker: str, lookback_days: int, target: float,
               interval_min: int, min_support: int) -> dict:
    df, meta = _build(ticker, lookback_days, target, interval_min)
    return {**meta, **_mine(df.copy(), min_support), "min_support": min_support}


def cached_studies() -> list[dict]:
    """One row per cached study: params + age, newest first (for the UI)."""
    out = []
    for v in _cache_load().values():
        p = v.get("params", {})
        out.append({**p, "computed_at": v.get("computed_at"),
                    "age_days": _days_since(v.get("computed_at", ""))})
    out.sort(key=lambda r: r.get("age_days", 10 ** 6))
    return out


# ------------------------------------------------------------------ API

def run(ticker: str = "QQQ", lookback_days: int = 365, target: float = 1.0,
        interval_min: int = 30, min_support: int = 40, force: bool = False) -> dict:
    """Deep-dive study for a (ticker, target, lookback, interval, min_support).

    Returns a cached result when one exists and is <= CACHE_TTL_DAYS old
    (unless force=True); otherwise computes it fresh and caches it. The
    response carries ``cached`` / ``computed_at`` / ``cache_age_days`` so the
    UI can show how old the study is.
    """
    ticker = ticker.upper().strip()
    _validate(target, lookback_days, interval_min, min_support)
    key = _cache_key(ticker, lookback_days, target, interval_min, min_support)
    entry = _cache_load().get(key)
    if entry and not force and _days_since(entry["computed_at"]) <= CACHE_TTL_DAYS:
        return {**entry["result"], "cached": True, "computed_at": entry["computed_at"],
                "cache_age_days": _days_since(entry["computed_at"])}
    result = _run_fresh(ticker, lookback_days, target, interval_min, min_support)
    params = {"ticker": ticker, "target": round(float(target), 2),
              "lookback_days": int(lookback_days), "interval_min": int(interval_min),
              "min_support": int(min_support)}
    computed_at = _cache_put(key, params, result)
    return {**result, "cached": False, "computed_at": computed_at, "cache_age_days": 0}


# ----------------------------------------------------- arming + live match

ARMED_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "patterns_armed.json"
)


def _parse_conds(s: str) -> dict:
    out = {}
    for part in s.split(" · "):
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip()
    return out


def _load_store() -> dict:
    """The armed store, keyed by ticker: {"tickers": {"QQQ": cfg, ...}}.

    Migrates the old single-ticker file ({ticker, patterns, ...}) forward so
    existing arms are preserved.
    """
    if not os.path.exists(ARMED_PATH):
        return {"tickers": {}}
    try:
        with open(ARMED_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception as exc:
        print(f"[patterns] could not read patterns_armed.json: {exc}")
        return {"tickers": {}}
    if "tickers" not in d and "patterns" in d:  # legacy single-ticker format
        d = {"tickers": {d["ticker"]: d}}
    d.setdefault("tickers", {})
    return d


def _save_store(store: dict) -> None:
    if store["tickers"]:
        with open(ARMED_PATH, "w", encoding="utf-8") as fh:
            json.dump(store, fh, indent=2)
    elif os.path.exists(ARMED_PATH):
        os.remove(ARMED_PATH)


def arm(ticker: str = "QQQ", lookback_days: int = 365, target: float = 1.0,
        interval_min: int = 30, min_support: int = 40, min_win: float = 70.0) -> dict:
    """Arm one ticker from its cached deep-dive study (never re-runs when a
    study already exists), keeping every pattern with win% >= min_win.

    Uses the cached study for these exact params if one exists (any age) so
    arming is instant and matches what the deep dive showed; only computes
    (once, then caches) when nothing is cached. Arming a ticker replaces only
    its own armed entry, so QQQ and AAPL can be armed at once and each is
    later matched against its own patterns.
    """
    ticker = ticker.upper().strip()
    _validate(target, lookback_days, interval_min, min_support)
    key = _cache_key(ticker, lookback_days, target, interval_min, min_support)
    entry = _cache_load().get(key)
    if entry:
        data = entry["result"]
        study_at, from_cache = entry["computed_at"], True
    else:
        data = _run_fresh(ticker, lookback_days, target, interval_min, min_support)
        params = {"ticker": ticker, "target": round(float(target), 2),
                  "lookback_days": int(lookback_days), "interval_min": int(interval_min),
                  "min_support": int(min_support)}
        study_at, from_cache = _cache_put(key, params, data), False
    picks = []
    for side, key in (("long", "long_patterns"), ("short", "short_patterns")):
        for p in data[key]:
            if p["pct"] is not None and p["pct"] >= min_win:
                picks.append({
                    "side": side, "conds": _parse_conds(p["conds"]), "label": p["conds"],
                    "win_pct": p["pct"], "train_pct": p["train_pct"], "test_pct": p["test_pct"],
                    "n": p["n"], "days": p["days"], "avg_mins": p["avg_mins"],
                })
    picks.sort(key=lambda p: -p["win_pct"])
    cfg = {
        "ticker": ticker, "target": round(target, 2), "interval_min": interval_min,
        "min_win": min_win, "lookback_days": lookback_days, "min_support": min_support,
        "armed_at": datetime.now(NY).isoformat(timespec="seconds"),
        "study_computed_at": study_at, "study_from_cache": from_cache,
        "date_range": data["date_range"], "days_used": data["days_used"],
        "baseline": data["baseline"], "patterns": picks,
    }
    store = _load_store()
    # One armed study per ticker: re-arming replaces the previous set, so the
    # monitor never matches the same ticker against two overlapping studies.
    cfg["replaced_previous"] = ticker in store["tickers"]
    store["tickers"][ticker] = cfg
    _save_store(store)
    return cfg


def armed() -> dict | None:
    """The whole armed store, or None when nothing is armed."""
    store = _load_store()
    return store if store["tickers"] else None


def armed_list() -> list[dict]:
    """Every armed ticker's config."""
    return list(_load_store()["tickers"].values())


def disarm(ticker: str | None = None) -> None:
    """Disarm one ticker, or all when ticker is None."""
    if ticker is None:
        if os.path.exists(ARMED_PATH):
            os.remove(ARMED_PATH)
        return
    store = _load_store()
    store["tickers"].pop(ticker.upper().strip(), None)
    _save_store(store)


def live_features(ticker: str) -> dict:
    """Discretize the ticker's conditions RIGHT NOW, same buckets as the study.

    Returns {"ready": False, "reason": ...} until enough of today's session
    has printed (opening range done + 7 bars for the momentum lookbacks).
    """
    now = datetime.now(NY)
    today = now.date()
    daily = load_history(ticker, days=90)
    prior = daily[daily.index.date < today] if not daily.empty else daily
    if len(prior) < 16:
        return {"ready": False, "reason": "insufficient daily history"}
    pdh, pdl = float(prior["High"].iloc[-1]), float(prior["Low"].iloc[-1])
    pdc = float(prior["Close"].iloc[-1])
    yret = (pdc / float(prior["Close"].iloc[-2]) - 1) * 100
    adr = float((prior["High"] - prior["Low"]).rolling(14).mean().iloc[-1])

    allbars = marketdata.bars(ticker, "5Min", days=10)
    if allbars.empty:
        return {"ready": False, "reason": "no intraday data"}
    delta = allbars["Close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = (100 - 100 / (1 + gain / loss.replace(0, np.nan))).fillna(50)

    g = allbars[allbars.index.date == today]
    hm = np.asarray(g.index.strftime("%H:%M"))
    if len(g) < 7 or (len(hm) and hm[0] != "09:30"):
        return {"ready": False, "reason": "session warming up (need 7+ bars from the 09:30 open)"}
    pre = g.iloc[np.asarray(hm < "10:00", dtype=bool)]
    if len(pre) < 5:
        return {"ready": False, "reason": "opening range not complete (before 10:00 ET)"}
    o = float(g["Open"].iloc[0])
    or_hi, or_lo = float(pre["High"].max()), float(pre["Low"].min())
    f30 = (float(pre["Close"].iloc[-1]) / o - 1) * 100
    tp = (g["High"] + g["Low"] + g["Close"]) / 3
    vol = g["Volume"].clip(lower=1)
    vwap = ((tp * vol).cumsum() / vol.cumsum()).to_numpy()
    closes, highs, lows = g["Close"].to_numpy(), g["High"].to_numpy(), g["Low"].to_numpy()
    i, P = len(g) - 1, float(g["Close"].iloc[-1])
    i6 = max(0, i - 6)
    rsi_now = float(rsi.loc[g.index[i]]) if g.index[i] in rsi.index else 50.0
    feats = _bucketize({
        "tod": hm[i], "P": P, "gap_pct": (o / pdc - 1) * 100,
        "or_hi": or_hi, "or_lo": or_lo,
        "dv": (P - vwap[i]) / vwap[i] * 100,
        "vslope": (vwap[i] - vwap[i6]) / P * 100,
        "rsi": rsi_now, "pdh": pdh, "pdl": pdl,
        "dow": now.strftime("%a"), "f30": f30, "yret": yret,
        "ru": (float(highs.max()) - float(lows.min())) / adr,
        "mom30": (P - float(closes[i6])) / float(closes[i6]) * 100,
    })
    return {
        "ready": True, "features": feats, "price": round(P, 2), "tod": hm[i],
        "source": allbars.attrs.get("source", "?"), "as_of": now.strftime("%H:%M:%S"),
    }


def match_live(features: dict, armed_patterns: list[dict]) -> list[dict]:
    """Armed patterns whose every condition equals the live feature set."""
    return [
        p for p in armed_patterns
        if all(features.get(k) == v for k, v in p["conds"].items())
    ]


def live_status(ticker: str | None = None) -> dict:
    """For each armed ticker: its config, a live snapshot, and which of its
    own patterns match right now. Restrict to one ticker when given."""
    cfgs = armed_list()
    if ticker:
        ticker = ticker.upper().strip()
        cfgs = [c for c in cfgs if c["ticker"] == ticker]
    if not cfgs:
        return {"armed": False, "tickers": []}
    out = []
    for cfg in cfgs:
        snap = live_features(cfg["ticker"])
        matches = match_live(snap["features"], cfg["patterns"]) if snap.get("ready") else []
        out.append({"config": cfg, "snapshot": snap, "matches": matches})
    return {"armed": True, "tickers": out}


# ------------------------------------------------------------- backtest

def _replay_ticker(cfg: dict, lo: str, hi: str) -> tuple[list[dict], set[str]]:
    """Replay ONE armed ticker over [lo, hi] exactly as the live monitor would.

    For every session in range it walks the 5-minute bars from the moment the
    session is "ready" (opening range done + 7 bars, ~10:00 ET) through the
    close, rebuilds the same live feature snapshot as ``live_features`` at each
    bar with no look-ahead, and fires a pattern the first time its conditions
    match that day (deduped per pattern/day, matching the monitor's INSERT OR
    IGNORE). Each fire is then settled to its +/-$ barrier or the session close
    with the same first-touch logic as the study, so results line up with the
    live "Pattern alerts fired" table. Event days are NOT excluded -- the
    monitor fires on them too, so the replay counts them too.
    """
    tk = cfg["ticker"]
    target = float(cfg["target"])
    start_d = date.fromisoformat(lo)
    # Only need bars from a little before `start` (RSI/daily warmup) to now --
    # far cheaper than the ticker's full arming lookback.
    span = (date.today() - start_d).days
    intr = marketdata.bars(tk, "5Min", days=span + 15)
    if intr.empty:
        raise ValueError(f"no intraday data for {tk}")
    daily = load_history(tk, days=span + 90)
    if daily.empty:
        raise ValueError(f"no daily data for {tk}")

    pdh, pdl, pdc = daily["High"].shift(1), daily["Low"].shift(1), daily["Close"].shift(1)
    yret = (daily["Close"].shift(1) / daily["Close"].shift(2) - 1) * 100
    adr = (daily["High"] - daily["Low"]).rolling(14).mean().shift(1)
    ctx = {
        ts.date().isoformat(): {"pdh": h, "pdl": l, "pdc": c, "yret": r, "adr": a}
        for ts, h, l, c, r, a in zip(daily.index, pdh, pdl, pdc, yret, adr)
    }

    delta = intr["Close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    intr = intr.copy()
    intr["rsi"] = (100 - 100 / (1 + gain / loss.replace(0, np.nan))).fillna(50)

    triggers: list[dict] = []
    sessions: set[str] = set()
    for day, g in intr.groupby(intr.index.date):
        ds = day.isoformat()
        if not (lo <= ds <= hi):
            continue
        c = ctx.get(ds)
        if c is None or pd.isna(c["adr"]) or pd.isna(c["pdc"]):
            continue
        hm = np.asarray(g.index.strftime("%H:%M"))
        pre = g.iloc[np.asarray(hm < "10:00", dtype=bool)]
        # Same readiness gate as live_features: full opening range + 7 bars.
        if len(g) < 7 or len(pre) < 5 or hm[0] != "09:30":
            continue
        sessions.add(ds)

        o = float(g["Open"].iloc[0])
        gap_pct = (o / c["pdc"] - 1) * 100
        or_hi, or_lo = float(pre["High"].max()), float(pre["Low"].min())
        f30 = (float(pre["Close"].iloc[-1]) / o - 1) * 100
        tp = (g["High"] + g["Low"] + g["Close"]) / 3
        vol = g["Volume"].clip(lower=1)
        vwap = ((tp * vol).cumsum() / vol.cumsum()).to_numpy()
        highs, lows = g["High"].to_numpy(), g["Low"].to_numpy()
        closes, rsis = g["Close"].to_numpy(), g["rsi"].to_numpy()
        hi_run = np.maximum.accumulate(highs)
        lo_run = np.minimum.accumulate(lows)
        dow = day.strftime("%a")

        fired: dict[str, dict] = {}  # label -> first match this day (dedup)
        for i in range(6, len(g)):          # ready from the 10:00 bar on
            if hm[i] < "10:00":
                continue
            i6 = max(0, i - 6)
            P = float(closes[i])
            feats = _bucketize({
                "tod": hm[i], "P": P, "gap_pct": gap_pct,
                "or_hi": or_hi, "or_lo": or_lo,
                "dv": (P - vwap[i]) / vwap[i] * 100,
                "vslope": (vwap[i] - vwap[i6]) / P * 100,
                "rsi": rsis[i], "pdh": c["pdh"], "pdl": c["pdl"],
                "dow": dow, "f30": f30, "yret": c["yret"],
                "ru": (hi_run[i] - lo_run[i]) / c["adr"],
                "mom30": (P - closes[i6]) / closes[i6] * 100,
            })
            for p in cfg["patterns"]:
                if p["label"] in fired:
                    continue
                if all(feats.get(k) == v for k, v in p["conds"].items()):
                    fired[p["label"]] = {"i": i, "tod": hm[i], "P": P, "pat": p}

        for m in fired.values():
            i, P, p = m["i"], m["P"], m["pat"]
            long = p["side"] == "long"
            tmask = (highs[i + 1:] >= P + target) if long else (lows[i + 1:] <= P - target)
            smask = (lows[i + 1:] <= P - target) if long else (highs[i + 1:] >= P + target)
            jt = int(np.argmax(tmask)) if tmask.any() else -1
            js = int(np.argmax(smask)) if smask.any() else -1
            if jt < 0 and js < 0:
                result, j = "session_close", -1
            elif js < 0 or (0 <= jt < js):
                result, j = "target", jt
            elif jt < 0 or js < jt:
                result, j = "stop", js
            else:
                result, j = "stop_ambig", jt
            mins = None if j < 0 else (g.index[i + 1 + j] - g.index[i]).total_seconds() / 60
            triggers.append({
                "date": ds, "tod": m["tod"], "ticker": tk,
                "side": p["side"], "label": p["label"], "win_pct": p["win_pct"],
                "result": result, "win": result == "target", "mins": _rnd(mins, 0),
            })
    return triggers, sessions


def backtest(start: str, end: str | None = None, ticker: str | None = None) -> dict:
    """What-if replay of the currently armed patterns over a past date range.

    Mirrors the live monitor bar-by-bar (see ``_replay_ticker``): answers "on
    these day(s), how many armed setups would have fired, and how many hit
    target first?" without the monitor having been running at the time.
    """
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end) if end else start_d
    if end_d < start_d:
        start_d, end_d = end_d, start_d
    lo, hi = start_d.isoformat(), end_d.isoformat()

    cfgs = armed_list()
    if ticker:
        ticker = ticker.upper().strip()
        cfgs = [c for c in cfgs if c["ticker"] == ticker]
    if not cfgs:
        return {"armed": False, "start": lo, "end": hi, "triggers": [],
                "per_ticker": [], "by_date": [], "summary": {}}

    triggers: list[dict] = []
    per_ticker: list[dict] = []
    for cfg in cfgs:
        tk = cfg["ticker"]
        try:
            tk_trigs, tk_sessions = _replay_ticker(cfg, lo, hi)
        except ValueError as exc:
            per_ticker.append({"ticker": tk, "error": str(exc), "triggers": 0, "sessions": 0})
            continue
        triggers.extend(tk_trigs)
        per_ticker.append({"ticker": tk, "sessions": len(tk_sessions),
                           "triggers": len(tk_trigs)})

    triggers.sort(key=lambda r: (r["date"], r["tod"], r["ticker"]))

    by_date: dict[str, dict] = {}
    for t in triggers:
        d = by_date.setdefault(t["date"], {"date": t["date"], "triggers": 0, "wins": 0})
        d["triggers"] += 1
        d["wins"] += int(t["win"])

    # Per-pattern rollup: how each armed pattern actually did over the window,
    # so a good pattern isn't hidden inside the blended realized rate.
    by_pat: dict[tuple, dict] = {}
    for t in triggers:
        key = (t["ticker"], t["side"], t["label"])
        g = by_pat.get(key)
        if g is None:
            g = by_pat[key] = {
                "ticker": t["ticker"], "side": t["side"], "label": t["label"],
                "predicted_pct": t["win_pct"], "triggers": 0, "wins": 0,
                "results": Counter(), "_mins": [], "dates": [],
            }
        g["triggers"] += 1
        g["wins"] += int(t["win"])
        g["results"][t["result"]] += 1
        g["dates"].append({"date": t["date"], "tod": t["tod"],
                           "result": t["result"], "win": t["win"]})
        if t["mins"] is not None:
            g["_mins"].append(t["mins"])
    by_pattern = []
    for g in by_pat.values():
        n, w = g["triggers"], g["wins"]
        mins = g.pop("_mins")
        by_pattern.append({
            **g, "results": dict(g["results"]),
            "realized_pct": round(100 * w / n, 1) if n else None,
            "edge": (round(100 * w / n - g["predicted_pct"], 1)
                     if n and g["predicted_pct"] is not None else None),
            "avg_mins": round(sum(mins) / len(mins)) if mins else None,
        })
    # Best realized first; ties broken by sample size then predicted rate.
    by_pattern.sort(key=lambda r: (-(r["realized_pct"] or 0), -r["triggers"],
                                   -(r["predicted_pct"] or 0)))

    total = len(triggers)
    wins = sum(1 for t in triggers if t["win"])
    preds = [t["win_pct"] for t in triggers if t["win_pct"] is not None]
    summary = {
        "total": total,
        "wins": wins,
        "realized_pct": round(100 * wins / total, 1) if total else None,
        "predicted_pct": round(sum(preds) / len(preds), 1) if preds else None,
        "sessions": len({t["date"] for t in triggers}),
        "results": dict(Counter(t["result"] for t in triggers)),
    }
    return {"armed": True, "start": lo, "end": hi, "triggers": triggers,
            "per_ticker": per_ticker, "by_pattern": by_pattern,
            "by_date": sorted(by_date.values(), key=lambda r: r["date"]),
            "summary": summary}
