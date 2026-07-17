"""The "divergence play" detector (Misc tab).

The setup, as swing analysts trade it: after a decline from a swing high
(HH) through a lower high (LH) down to a low (LL), price prints a LOWER low
while RSI prints a HIGHER low -- bullish divergence: momentum turned up
before price bottomed. The bounce is then measured with a Fibonacci
retracement of the LL -> LH swing: the 50% equilibrium (EQ) and the golden
pocket (0.618-0.786) are the decision zones, and LH / HH are the structural
targets above. The bearish mirror (price higher high, RSI lower high, fib
measured down from the top) is detected when the window's most recent
extreme is a high instead of a low.

analyze() answers "does this exist on <ticker> right now?" with the full
structure (dates + prices), the divergence evidence, MACD confirmation,
every fib level, where price sits in the swing, and the next targets --
in a payload the Misc tab draws directly onto the chart.

Same data and helpers as the Analyzer chart (last ~260 daily bars, pivot
detection with k=3, Wilder RSI-14), so what is marked here lines up
exactly with the candles rendered next to it.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from .analyzer import _bars_for, _pivot_indices, _rsi_series, _times, _trama, _trendlines

FIBS = (0.236, 0.382, 0.5, 0.618, 0.786)
TRAMA_LEN = 24

# The timeframe nest: bigger picture (sets structure/trend) down to timing.
PICTURES = [
    ("1d", "Daily — bigger picture"),
    ("4h", "4-hour — smaller picture"),
    ("15m", "15-min — timing"),
]


def _trend(df: pd.DataFrame) -> dict:
    """Overall price-trend read from EMA structure and slope."""
    close = df["Close"]
    ema = lambda n: close.ewm(span=n, adjust=False).mean()
    e20s, e50s = ema(20), ema(50)
    e20, e50 = float(e20s.iloc[-1]), float(e50s.iloc[-1])
    e200 = float(ema(200).iloc[-1]) if len(close) >= 200 else None
    P = float(close.iloc[-1])
    slope = e20 - float(e20s.iloc[-6])  # EMA20 change over the last 5 bars
    up = P > e50 and e20 > e50 and slope > 0
    down = P < e50 and e20 < e50 and slope < 0
    direction = "up" if up else "down" if down else "sideways"
    parts = [
        f"price {'above' if P > e50 else 'below'} EMA50 ({e50:.2f})",
        f"EMA20 {'above' if e20 > e50 else 'below'} EMA50",
        f"EMA20 {'rising' if slope > 0 else 'falling' if slope < 0 else 'flat'}",
    ]
    if e200 is not None:
        parts.append(f"{'above' if P > e200 else 'below'} EMA200 ({e200:.2f})")
    return {
        "direction": direction, "text": "; ".join(parts),
        "ema20": round(e20, 2), "ema50": round(e50, 2),
        "ema200": round(e200, 2) if e200 is not None else None,
    }


def _overlays(df: pd.DataFrame, times: list, anchor_date: str) -> dict:
    """TRAMA(24), anchored VWAP from the swing low, and auto trendlines."""
    trama = _trama(df, TRAMA_LEN)
    trama_pts = [{"time": t, "value": round(float(v), 2)}
                 for t, v in zip(times, trama) if pd.notna(v)]
    # Anchored VWAP: cumulative typical-price * volume from the swing-low bar
    # onward -- the average price paid since the bottom, a dynamic support.
    a = times.index(anchor_date) if anchor_date in times else 0
    tp = ((df["High"] + df["Low"] + df["Close"]) / 3.0).to_numpy(float)
    vol = df["Volume"].to_numpy(float)
    cum_pv = np.cumsum((tp * vol)[a:])
    cum_v = np.cumsum(vol[a:])
    vwap = cum_pv / np.where(cum_v == 0, np.nan, cum_v)
    vwap_pts = [{"time": times[a + i], "value": round(float(v), 2)}
                for i, v in enumerate(vwap) if not np.isnan(v)]
    return {
        "trama24": trama_pts,
        "vwap": {"anchor_date": anchor_date, "points": vwap_pts},
        "trendlines": _trendlines(df, times),
    }


def _macd(close: np.ndarray) -> tuple[float, float]:
    c = pd.Series(close)
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    signal = macd.ewm(span=9, adjust=False).mean()
    return float(macd.iloc[-1]), float(signal.iloc[-1])


def _fib_label(f: float, direction: str) -> str:
    if f == 0.5:
        return "EQ (50%)"
    if f == 0.618:
        return "Fib 0.618 — golden pocket " + ("bottom" if direction == "bullish" else "top")
    if f == 0.786:
        return "Fib 0.786 — golden pocket " + ("top" if direction == "bullish" else "bottom")
    return f"Fib {f}"


def _tf_scan(ticker: str, tf: str, label: str) -> dict:
    """Divergence read on one timeframe: the freshest regular/hidden
    divergence at the most recent swing extreme, plus trend and RSI now.

    The divergence line's times are encoded exactly as the Analyzer chart
    encodes that timeframe (date string on 1D, epoch on intraday), so the
    Misc chart can draw it on the matching candles without misalignment.
    """
    df = _bars_for(ticker, tf)
    if df.empty or len(df) < 40:
        return {"tf": tf, "label": label, "ok": False, "reason": "not enough data"}
    times = _times(df.index, tf)
    hi, lo = df["High"].to_numpy(float), df["Low"].to_numpy(float)
    close = df["Close"].to_numpy(float)
    rsi = _rsi_series(df["Close"], 14).to_numpy(float)
    piv_hi = _pivot_indices(hi, 3, "high")
    piv_lo = _pivot_indices(lo, 3, "low")
    bull = _divergence(lo, rsi, piv_lo, piv_lo[-1], times, "bullish") if piv_lo else {"found": False}
    bear = _divergence(hi, rsi, piv_hi, piv_hi[-1], times, "bearish") if piv_hi else {"found": False}
    last_lo = piv_lo[-1] if piv_lo else -1
    last_hi = piv_hi[-1] if piv_hi else -1
    if bull.get("found") and (not bear.get("found") or last_lo >= last_hi):
        chosen, direction = bull, "bullish"
    elif bear.get("found"):
        chosen, direction = bear, "bearish"
    else:
        chosen, direction = {"found": False}, ("bullish" if last_lo >= last_hi else "bearish")
    return {
        "tf": tf, "label": label, "ok": True, "direction": direction,
        "found": bool(chosen.get("found")), "kind": chosen.get("kind"),
        "text": chosen.get("text"), "price": round(float(close[-1]), 2),
        "rsi_now": round(float(rsi[-1]), 1) if not np.isnan(rsi[-1]) else None,
        "trend": _trend(df)["direction"], "source": df.attrs.get("source", "yfinance"),
        "divergence": {"found": bool(chosen.get("found")),
                       "price_points": chosen.get("price_points", []),
                       "rsi_points": chosen.get("rsi_points", [])},
    }


def _short_tf(label: str) -> str:
    return label.split(" ")[0]


def _synthesize(daily: dict, scans: list[dict]) -> dict:
    """How the timeframes stack: the heart of 'smaller vs bigger picture'.

    A smaller-picture divergence is only high-conviction when it AGREES with
    the bigger picture's direction and, ideally, fires at a bigger-picture
    decision level (EQ / golden pocket). Against the bigger trend it is
    usually just a pullback."""
    bd = daily["direction"]
    zone = daily["position"]["zone"]
    at_level = any(w in zone for w in ("golden pocket", "EQ", "equilibrium"))
    smalls = [s for s in scans[1:] if s.get("ok")]
    found_small = [s for s in smalls if s["found"]]
    aligned = [s for s in found_small if s["direction"] == bd]
    conflict = [s for s in found_small if s["direction"] != bd]

    if daily["exists"] and aligned:
        stack = "stacked"
        headline = (f"Nested {bd} divergence — the daily (bigger picture) setup is echoed on "
                    f"{', '.join(_short_tf(s['label']) for s in aligned)}. This stack is exactly what "
                    "the analyst means: the smaller picture confirms the bigger one.")
    elif found_small and not daily["exists"]:
        stack = "leading"
        headline = (f"Smaller picture is leading — divergence has printed on "
                    f"{', '.join(_short_tf(s['label']) for s in found_small)} but the daily hasn't "
                    "confirmed yet. An early heads-up to watch, not a bigger-picture signal on its own.")
    elif conflict and not aligned:
        stack = "conflict"
        headline = ("Timeframe conflict — the smaller picture diverges AGAINST the daily direction. "
                    "That is usually a pullback inside the bigger trend, not the turn. Low conviction.")
    elif daily["exists"]:
        stack = "big_only"
        headline = ("Bigger picture only — the daily shows the divergence but the smaller timeframes are "
                    "quiet. The move may already be underway; wait for a smaller-picture pullback + fresh "
                    "divergence for a cleaner entry.")
    else:
        stack = "none"
        headline = "No divergence stack right now across daily / 4H / 15M."

    bullets = []
    for s in scans:
        if not s.get("ok"):
            bullets.append(f"{s['label']}: no data on this timeframe")
        elif s["found"]:
            bullets.append(f"{s['label']}: {s['kind']} {s['direction']} divergence — "
                           f"RSI {s['rsi_now']}, {s['trend']} trend")
        else:
            bullets.append(f"{s['label']}: no active divergence — RSI {s['rsi_now']}, {s['trend']} trend")
    if at_level:
        bullets.append(f"Location: price sits at a bigger-picture decision level ({zone}). A smaller-picture "
                       "divergence here carries extra weight — level + momentum agree.")
    return {"stack": stack, "headline": headline, "bullets": bullets, "at_level": at_level,
            "aligned": len(aligned), "conflict": len(conflict)}


def _score(res: dict) -> int:
    """Signed buy/sell score, −100 (strong sell) … +100 (strong buy).

    The divergence is the core; everything else raises or lowers conviction:
    confirmation (MACD + reclaimed EQ), the multi-timeframe stack when
    present, sitting at a decision level, and trend agreement.
    """
    d = res["direction"]
    sign = 1 if d == "bullish" else -1
    dv = res["divergence"]
    mag = 0
    if dv.get("found"):
        mag += 40 if dv.get("kind") == "hidden" else 35
    if res.get("confirmed"):
        mag += 20
    if res["macd"]["bullish"] == (d == "bullish"):
        mag += 10
    if any(w in res["position"]["zone"] for w in ("golden pocket", "EQ", "equilibrium")):
        mag += 8
    tr = res.get("trend", {}).get("direction")
    if tr == ("up" if d == "bullish" else "down"):
        mag += 10
    elif tr == ("down" if d == "bullish" else "up"):
        mag -= 10
    stack = (res.get("mtf_read") or {}).get("stack")
    mag += {"stacked": 20, "leading": 12, "big_only": 5, "conflict": -20}.get(stack, 0)
    return int(sign * max(0, min(100, mag)))


def _score_label(score: int) -> str:
    a = abs(score)
    tier = "Strong " if a >= 60 else "" if a >= 30 else "Watch "
    if a < 12:
        return "Neutral"
    side = "Buy" if score > 0 else "Sell"
    return (tier + side).strip() if tier != "Watch " else f"Watch ({'bull' if score > 0 else 'bear'})"


def analyze(ticker: str, mtf: bool = True) -> dict:
    df = _bars_for(ticker, "1d")
    if df.empty or len(df) < 80:
        return {"error": f"not enough daily history for {ticker}"}
    times = [str(ts.date()) for ts in df.index]
    hi = df["High"].to_numpy(float)
    lo = df["Low"].to_numpy(float)
    close = df["Close"].to_numpy(float)
    rsi = _rsi_series(df["Close"], 14).to_numpy(float)

    piv_hi = _pivot_indices(hi, 3, "high")
    piv_lo = _pivot_indices(lo, 3, "low")
    if not piv_hi or not piv_lo:
        return {"error": f"no swing structure found for {ticker}"}

    hh_i = max(piv_hi, key=lambda i: hi[i])   # window's highest swing high
    P = float(close[-1])
    today = {
        "price": round(P, 2),
        "change_pct": round((P / float(close[-2]) - 1) * 100, 2),
        "date": times[-1],
    }
    macd_v, macd_s = _macd(close)
    macd = {"macd": round(macd_v, 2), "signal": round(macd_s, 2), "bullish": macd_v > macd_s}

    # The play lives on the most recent completed leg, not the window-global
    # extremes ("smaller picture inside the bigger picture"): if any swing
    # low printed AFTER the highest high, the recent leg is the decline off
    # that high -- analyze its recovery (bullish mirror). Only when the high
    # is the latest extreme is the question "is this top diverging?".
    lows_after = [i for i in piv_lo if i > hh_i]
    if lows_after:
        ll_i = min(lows_after, key=lambda i: lo[i])  # deepest low since HH
        res = _bullish(ticker, times, hi, lo, rsi, piv_hi, piv_lo, hh_i, ll_i, P, today, macd)
    else:
        ll_i = min(piv_lo, key=lambda i: lo[i])      # rally base before HH
        res = _bearish(ticker, times, hi, lo, rsi, piv_hi, piv_lo, hh_i, ll_i, P, today, macd)
    if "error" in res:
        return res
    res["overlays"] = _overlays(df, times, res["fib"]["base_date"])
    res["trend"] = _trend(df)

    # Multi-timeframe stack (skipped in bulk scans for speed): the daily row
    # comes from the full analysis above (consistent with the verdict); 4H and
    # 15M are fresh divergence scans.
    if mtf:
        daily_row = {
            "tf": "1d", "label": PICTURES[0][1], "ok": True, "direction": res["direction"],
            "found": res["divergence"]["found"], "kind": res["divergence"].get("kind"),
            "text": res["divergence"].get("text"), "price": res["today"]["price"],
            "rsi_now": round(float(rsi[-1]), 1) if not np.isnan(rsi[-1]) else None,
            "trend": res["trend"]["direction"], "source": df.attrs.get("source", "yfinance"),
            "divergence": {"found": res["divergence"]["found"],
                           "price_points": res["divergence"].get("price_points", []),
                           "rsi_points": res["divergence"].get("rsi_points", [])},
        }
        scans = [daily_row]
        for tf, label in PICTURES[1:]:
            try:
                scans.append(_tf_scan(ticker, tf, label))
            except Exception as exc:
                scans.append({"tf": tf, "label": label, "ok": False, "reason": str(exc)})
        res["mtf"] = scans
        res["mtf_read"] = _synthesize(res, scans)

    res["score"] = _score(res)
    res["score_label"] = _score_label(res["score"])
    return res


def _scan_one(ticker: str) -> dict:
    try:
        return analyze(ticker, mtf=False)
    except Exception as exc:
        return {"ticker": ticker, "error": str(exc)}


def scan(tickers: list[str], only_divergence: bool = True, max_workers: int = 8) -> list[dict]:
    """Scan a watchlist: one compact row per ticker, ranked by |score|.

    Daily-only (no 4H/15M) so a 100-name list stays fast; open a single ticker
    in the detail view for the full multi-timeframe stack. When
    only_divergence is True, names with no daily divergence are dropped.
    """
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for r in pool.map(_scan_one, tickers):
            if "error" in r or "divergence" not in r:
                continue
            if only_divergence and not r["divergence"]["found"]:
                continue
            rows.append({
                "ticker": r["ticker"], "direction": r["direction"],
                "score": r["score"], "label": r["score_label"],
                "found": r["divergence"]["found"], "kind": r["divergence"].get("kind"),
                "summary": r["divergence"].get("text"),
                "price": r["today"]["price"], "change_pct": r["today"]["change_pct"],
                "zone": r["position"]["zone"], "pct_of_swing": r["position"]["pct_of_swing"],
                "confirmed": r["confirmed"], "exists": r["exists"],
                "trend": r["trend"]["direction"], "macd_bullish": r["macd"]["bullish"],
                "gp": r["fib"]["gp"], "targets": r["targets"][:2],
            })
    rows.sort(key=lambda x: -abs(x["score"]))
    return rows


def _div_payload(prices, rsi, times, prior_t, ext_t, kind, text) -> dict:
    """prior_t / ext_t are (price_pivot_idx, rsi_trough_idx, rsi_value): the
    price line anchors on the price pivot, the RSI line on its nearby trough."""
    j, j_r_i, j_r = prior_t
    at, at_r_i, at_r = ext_t
    return {
        "found": True, "kind": kind, "text": text,
        "price_points": [{"time": times[j], "value": round(float(prices[j]), 2)},
                         {"time": times[at], "value": round(float(prices[at]), 2)}],
        "rsi_points": [{"time": times[j_r_i], "value": round(float(j_r), 2)},
                       {"time": times[at_r_i], "value": round(float(at_r), 2)}],
        "prior": {"date": times[j], "price": round(float(prices[j]), 2), "rsi": round(float(j_r), 1)},
        "extreme": {"date": times[at], "price": round(float(prices[at]), 2), "rsi": round(float(at_r), 1)},
    }


def _divergence(prices: np.ndarray, rsi: np.ndarray, pivots: list[int],
                at: int, times: list, mode: str) -> dict:
    """Divergence at the extreme `at`, two readings, as analysts use them:

    Regular (smaller picture) -- vs the last few pivots of the same leg:
    bullish = lower price low with higher RSI; bearish = mirror. Momentum
    turned before price did.

    Hidden (bigger picture) -- vs the prior MAJOR extreme in the window:
    bullish = price held a HIGHER low while RSI printed a LOWER low
    (trend-continuation); bearish = mirror. Checked when no regular
    divergence exists on the small picture.
    """
    # The momentum reading for a price low is the RSI TROUGH near it (the RSI
    # extreme can lag the price extreme by a bar or two); mirror for highs.
    # Reading RSI on the exact pivot bar misses divergences analysts see.
    def rsi_ext(j: int) -> tuple[int, float]:
        s, e = max(0, j - 3), min(len(rsi), j + 4)
        win = rsi[s:e]
        if np.all(np.isnan(win)):
            return j, float("nan")
        k = s + (int(np.nanargmin(win)) if mode == "bullish" else int(np.nanargmax(win)))
        return k, float(rsi[k])

    at_k, at_r = rsi_ext(at)
    if np.isnan(at_r):
        return {"found": False}
    prior = [i for i in pivots if i < at]
    for j in reversed(prior[-4:]):  # regular, most recent comparison first
        j_k, j_r = rsi_ext(j)
        if np.isnan(j_r):
            continue
        if mode == "bullish" and prices[at] < prices[j] and at_r > j_r + 1:
            return _div_payload(prices, rsi, times, (j, j_k, j_r), (at, at_k, at_r), "regular",
                                f"Bullish divergence: price low {prices[at]:.2f} ({times[at]}) "
                                f"under {prices[j]:.2f} ({times[j]}) while RSI held a higher low "
                                f"{j_r:.1f} → {at_r:.1f} — momentum turned before price")
        if mode == "bearish" and prices[at] > prices[j] and at_r < j_r - 1:
            return _div_payload(prices, rsi, times, (j, j_k, j_r), (at, at_k, at_r), "regular",
                                f"Bearish divergence: price high {prices[at]:.2f} ({times[at]}) "
                                f"over {prices[j]:.2f} ({times[j]}) while RSI set a lower high "
                                f"{j_r:.1f} → {at_r:.1f} — momentum turned before price")
    if prior:  # hidden, vs the prior major extreme (bigger picture)
        if mode == "bullish":
            j = min(prior, key=lambda i: prices[i])
            j_k, j_r = rsi_ext(j)
            if not np.isnan(j_r) and prices[at] > prices[j] and at_r < j_r - 1:
                return _div_payload(prices, rsi, times, (j, j_k, j_r), (at, at_k, at_r), "hidden",
                                    f"Hidden bullish divergence (bigger picture): price held a "
                                    f"higher low {prices[at]:.2f} ({times[at]}) vs {prices[j]:.2f} "
                                    f"({times[j]}) while RSI made a lower low "
                                    f"{j_r:.1f} → {at_r:.1f} — trend-continuation signal")
        else:
            j = max(prior, key=lambda i: prices[i])
            j_k, j_r = rsi_ext(j)
            if not np.isnan(j_r) and prices[at] < prices[j] and at_r > j_r + 1:
                return _div_payload(prices, rsi, times, (j, j_k, j_r), (at, at_k, at_r), "hidden",
                                    f"Hidden bearish divergence (bigger picture): price set a "
                                    f"lower high {prices[at]:.2f} ({times[at]}) vs {prices[j]:.2f} "
                                    f"({times[j]}) while RSI made a higher high "
                                    f"{j_r:.1f} → {at_r:.1f} — trend-continuation signal")
    return {"found": False}


def _bullish(ticker, times, hi, lo, rsi, piv_hi, piv_lo, hh_i, ll_i, P, today, macd) -> dict:
    # Swing top for the fib: the highest lower-high between HH and LL (the
    # top of the final leg down); when the decline ran straight off HH, HH
    # itself is the swing top.
    between = [i for i in piv_hi if hh_i < i < ll_i]
    lh_i = max(between, key=lambda i: hi[i]) if between else hh_i
    base, top = float(lo[ll_i]), float(hi[lh_i])
    if top <= base:
        return {"error": "degenerate swing (top below base)"}

    div = _divergence(lo, rsi, piv_lo, ll_i, times, "bullish")
    levels = [{"f": f, "price": round(base + f * (top - base), 2),
               "label": _fib_label(f, "bullish")} for f in FIBS]
    eq, gp_lo, gp_hi = levels[2]["price"], levels[3]["price"], levels[4]["price"]
    frac = (P - base) / (top - base)

    if P < base:
        zone = "below the divergence low — the swing low did not hold"
    elif frac < 0.5:
        zone = "below equilibrium — bounce not yet confirmed"
    elif frac < 0.618:
        zone = "between EQ and the golden pocket — reclaim in progress"
    elif frac <= 0.786:
        zone = "inside the golden pocket — the battleground"
    elif P <= top:
        zone = "above the golden pocket, under LH"
    elif P <= float(hi[hh_i]):
        zone = "between LH and HH"
    else:
        zone = "above HH — full recovery"

    targets = [{"label": lv["label"], "price": lv["price"]}
               for lv in levels if lv["price"] > P]
    if top > P:
        targets.append({"label": "LH (swing top)", "price": round(top, 2)})
    if float(hi[hh_i]) > P and hh_i != lh_i:
        targets.append({"label": "HH", "price": round(float(hi[hh_i]), 2)})
    targets = sorted(targets, key=lambda t: t["price"])[:4]

    exists = bool(div["found"]) and P > base
    confirmed = exists and frac >= 0.5 and macd["bullish"]
    if confirmed:
        verdict = f"YES — bullish divergence play detected and in motion on {ticker}"
    elif exists:
        verdict = f"YES — bullish divergence found on {ticker}, not yet confirmed"
    elif div["found"]:
        verdict = f"NO — divergence printed but price broke the swing low on {ticker}"
    else:
        verdict = f"NO — no bullish divergence at the recent low on {ticker}"

    reasons = [
        div["text"] if div["found"] else
        "No divergence: RSI did not make a higher low against a lower price low",
        f"MACD {macd['macd']} vs signal {macd['signal']} — "
        + ("bullish cross ✓" if macd["bullish"] else "still below signal ✗"),
        f"Price {today['price']} ({today['change_pct']:+}% today) has recovered "
        f"{frac * 100:.0f}% of the {base:.2f} → {top:.2f} swing — {zone}",
    ]
    if targets:
        reasons.append("Next levels above: " + ", ".join(f"{t['price']} ({t['label']})" for t in targets))

    return {
        "ticker": ticker, "direction": "bullish", "exists": exists,
        "confirmed": confirmed, "verdict": verdict, "reasons": reasons,
        "today": today, "macd": macd, "divergence": div,
        "structure": [
            {"key": "HH", "label": "Swing high (HH)", "date": times[hh_i],
             "price": round(float(hi[hh_i]), 2)},
            {"key": "LH", "label": "Lower high (LH — swing top)", "date": times[lh_i],
             "price": round(top, 2)},
            {"key": "LL", "label": "Swing low (LL — divergence low)", "date": times[ll_i],
             "price": round(base, 2)},
        ],
        "fib": {"base": round(base, 2), "top": round(top, 2),
                "base_date": times[ll_i], "top_date": times[lh_i],
                "levels": levels, "eq": eq, "gp": [gp_lo, gp_hi]},
        "position": {"pct_of_swing": round(frac * 100, 1), "zone": zone},
        "targets": targets,
    }


def _bearish(ticker, times, hi, lo, rsi, piv_hi, piv_lo, hh_i, ll_i, P, today, macd) -> dict:
    # Mirror image: rally from LL through a higher low (HL) to the recent HH;
    # divergence checked at the top, fib retracement measured DOWN from it.
    between = [i for i in piv_lo if ll_i < i < hh_i]
    hl_i = min(between, key=lambda i: lo[i]) if between else ll_i
    base, top = float(lo[hl_i]), float(hi[hh_i])
    if top <= base:
        return {"error": "degenerate swing (top below base)"}

    div = _divergence(hi, rsi, piv_hi, hh_i, times, "bearish")
    levels = [{"f": f, "price": round(top - f * (top - base), 2),
               "label": _fib_label(f, "bearish")} for f in FIBS]
    eq, gp_hi, gp_lo = levels[2]["price"], levels[3]["price"], levels[4]["price"]
    frac = (top - P) / (top - base)

    if P > top:
        zone = "above the divergence high — the swing high did not hold"
    elif frac < 0.236:
        zone = "just under the high — pullback not yet confirmed"
    elif frac < 0.5:
        zone = "retracing — above equilibrium"
    elif frac < 0.618:
        zone = "through EQ, approaching the golden pocket"
    elif frac <= 0.786:
        zone = "inside the golden pocket — the battleground"
    else:
        zone = "below the golden pocket — deep retracement toward the swing base"

    targets = [{"label": lv["label"], "price": lv["price"]}
               for lv in levels if lv["price"] < P]
    if base < P:
        targets.append({"label": "HL (swing base)", "price": round(base, 2)})
    if float(lo[ll_i]) < P and ll_i != hl_i:
        targets.append({"label": "LL", "price": round(float(lo[ll_i]), 2)})
    targets = sorted(targets, key=lambda t: -t["price"])[:4]

    exists = bool(div["found"]) and P < top
    confirmed = exists and frac >= 0.382 and not macd["bullish"]
    if confirmed:
        verdict = f"YES — bearish divergence play detected and in motion on {ticker}"
    elif exists:
        verdict = f"YES — bearish divergence at the high on {ticker}, not yet confirmed"
    elif div["found"]:
        verdict = f"NO — divergence printed but price pushed above the swing high on {ticker}"
    else:
        verdict = f"NO — no bearish divergence at the recent high on {ticker} (uptrend intact)"

    reasons = [
        div["text"] if div["found"] else
        "No divergence: RSI did not make a lower high against a higher price high",
        f"MACD {macd['macd']} vs signal {macd['signal']} — "
        + ("still above signal ✗ (bulls holding)" if macd["bullish"] else "bearish cross ✓"),
        f"Price {today['price']} ({today['change_pct']:+}% today) has retraced "
        f"{frac * 100:.0f}% of the {base:.2f} → {top:.2f} rally — {zone}",
    ]
    if targets:
        reasons.append("Next levels below: " + ", ".join(f"{t['price']} ({t['label']})" for t in targets))

    return {
        "ticker": ticker, "direction": "bearish", "exists": exists,
        "confirmed": confirmed, "verdict": verdict, "reasons": reasons,
        "today": today, "macd": macd, "divergence": div,
        "structure": [
            {"key": "LL", "label": "Prior swing low (LL)", "date": times[ll_i],
             "price": round(float(lo[ll_i]), 2)},
            {"key": "HL", "label": "Higher low (HL — swing base)", "date": times[hl_i],
             "price": round(base, 2)},
            {"key": "HH", "label": "Swing high (HH — divergence high)", "date": times[hh_i],
             "price": round(top, 2)},
        ],
        "fib": {"base": round(base, 2), "top": round(top, 2),
                "base_date": times[hl_i], "top_date": times[hh_i],
                "levels": levels, "eq": eq, "gp": sorted([gp_lo, gp_hi])},
        "position": {"pct_of_swing": round(frac * 100, 1), "zone": zone},
        "targets": targets,
    }
