"""On-demand ticker analyzer: swing + day-trade scores, price projections,
and reversal detection.

For each ticker two views are built from the same indicator battery used by
the strategies (indicators.enrich), plus fresh 5-minute session data:

  Swing view  -- daily bars: trend structure (SMA/EMA stack), momentum
                 (MACD, RSI14, RSI2), volatility position (Bollinger),
                 volume, breakouts and pullback quality. Each component
                 contributes signed points with a reason string; the sum
                 maps to a verdict (Strong Buy .. Strong Sell).
  Day view    -- 5-minute bars: position vs VWAP, opening range, first-30-min
                 direction, gap behaviour, intraday RSI and relative volume.

Projections are statistical cones, not oracles: the point estimate comes
from short-horizon drift (intraday regression slope for end-of-day, 20-day
mean drift for end-of-week) and the band is +/-1 sigma of realized
volatility scaled by the remaining horizon. Roughly 2 times out of 3 the
close should land inside the band -- treat it as a range, never a promise.

Reversal detection is shared by the tab and the alert monitor:
RSI(14) crossing back through 30/70, MACD histogram inflection, 20-day
price/RSI divergence, reversal candles after a 3-bar run, and intraday
VWAP reclaim/loss.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import marketdata
from .data import load_history

NY = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------
# Data helpers
# --------------------------------------------------------------------------

def _intraday(ticker: str) -> pd.DataFrame:
    """Today's 5-minute session bars: Alpaca IEX first, yfinance fallback."""
    return marketdata.intraday(ticker, yf_symbol=ticker)


def _vwap(bars: pd.DataFrame) -> pd.Series:
    tp = (bars["High"] + bars["Low"] + bars["Close"]) / 3.0
    vol = bars["Volume"]
    if float(vol.sum()) > 0:
        return (tp * vol).cumsum() / vol.cumsum()
    return tp.expanding().mean()


def _rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    ag = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    al = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = ag / al.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(100.0).where(ag.notna(), np.nan)


def _verdict(score: float) -> str:
    if score >= 40:
        return "Strong Buy"
    if score >= 15:
        return "Buy"
    if score > -15:
        return "Neutral"
    if score > -40:
        return "Sell"
    return "Strong Sell"


# --------------------------------------------------------------------------
# Swing view (daily bars)
# --------------------------------------------------------------------------

def _swing_view(df: pd.DataFrame) -> dict:
    row = df.iloc[-1]
    pts: list[tuple[int, str]] = []

    def add(p: int, why: str):
        pts.append((p, why))

    c = row["Close"]
    # -- trend structure
    if pd.notna(row["sma200"]):
        add(15 if c > row["sma200"] else -15,
            f"{'Above' if c > row['sma200'] else 'Below'} 200-day SMA (long-term trend)")
    if pd.notna(row["sma50"]) and pd.notna(row["sma200"]):
        add(10 if row["sma50"] > row["sma200"] else -10,
            f"SMA50 {'>' if row['sma50'] > row['sma200'] else '<'} SMA200 "
            f"({'golden' if row['sma50'] > row['sma200'] else 'death'}-cross regime)")
    if pd.notna(row["sma20"]):
        add(5 if c > row["sma20"] else -5,
            f"{'Above' if c > row['sma20'] else 'Below'} 20-day SMA (short-term trend)")
    if row.get("trend_up"):
        add(10, "EMA5 > EMA21 > EMA50 (aligned uptrend)")
    elif (pd.notna(row.get("ema5")) and pd.notna(row.get("ema50"))
          and row["ema5"] < row["ema21"] < row["ema50"]):
        add(-10, "EMA5 < EMA21 < EMA50 (aligned downtrend)")

    # -- momentum
    if pd.notna(row["macd"]) and pd.notna(row["macd_signal"]):
        add(8 if row["macd"] > row["macd_signal"] else -8,
            f"MACD {'above' if row['macd'] > row['macd_signal'] else 'below'} signal line")
        if bool(df["macd_cross_up"].iloc[-5:].any()):
            add(5, "MACD bullish cross within the last 5 sessions")
        elif bool(df["macd_cross_down"].iloc[-5:].any()):
            add(-5, "MACD bearish cross within the last 5 sessions")
    rsi14 = row["rsi14"]
    if pd.notna(rsi14):
        in_uptrend = pd.notna(row["sma200"]) and c > row["sma200"]
        if rsi14 >= 70:
            add(-10, f"RSI(14) {rsi14:.0f} overbought")
        elif rsi14 <= 30:
            add(10 if in_uptrend else 3,
                f"RSI(14) {rsi14:.0f} oversold{' in an uptrend (snapback zone)' if in_uptrend else ''}")
        elif 30 < rsi14 < 45 and in_uptrend:
            add(8, f"RSI(14) {rsi14:.0f}: pullback territory inside an uptrend")
    if pd.notna(row["rsi2"]) and row["rsi2"] < 10 and pd.notna(row["sma200"]) and c > row["sma200"]:
        add(8, f"RSI(2) {row['rsi2']:.0f}: deep short-term washout above the 200-day (mean-reversion buy zone)")

    # -- volatility position
    if pd.notna(row["bb_lower"]) and c < row["bb_lower"] and pd.notna(row["sma200"]) and c > row["sma200"]:
        add(6, "Close below the lower Bollinger band in an uptrend (stretched, snapback setup)")
    if pd.notna(row["bb_upper"]) and c > row["bb_upper"]:
        add(-6, "Close above the upper Bollinger band (stretched to the upside)")

    # -- volume
    rel_vol = row.get("rel_vol20")
    if pd.notna(rel_vol) and rel_vol > 1.5:
        up_day = c >= df["Close"].iloc[-2]
        add(6 if up_day else -6,
            f"Volume {rel_vol:.1f}x average on a{'n up' if up_day else ' down'} day")

    # -- breakouts / breakdowns
    if pd.notna(row.get("hi20_prior")) and c > row["hi20_prior"]:
        add(8, "New 20-day closing high (breakout)")
    if pd.notna(row.get("lo10_prior")) and c < row["lo10_prior"]:
        add(-8, "Below the prior 10-day low (breakdown)")

    # -- pullback quality (CRWV-style)
    pb = row.get("pullback_pct")
    if pd.notna(pb) and row.get("trend_up") and 10 <= pb <= 30:
        add(8, f"Healthy {pb:.0f}% pullback from the 60-day high inside an uptrend")
    elif pd.notna(pb) and pb > 40:
        add(-6, f"{pb:.0f}% below the 60-day high (damaged chart)")

    score = sum(p for p, _ in pts)
    return {
        "score": int(score),
        "verdict": _verdict(score),
        "reasons": [f"{'+' if p > 0 else ''}{p} {why}" for p, why in
                    sorted(pts, key=lambda t: -abs(t[0]))],
        "levels": {
            "close": float(c),
            "sma20": float(row["sma20"]) if pd.notna(row["sma20"]) else None,
            "sma50": float(row["sma50"]) if pd.notna(row["sma50"]) else None,
            "sma200": float(row["sma200"]) if pd.notna(row["sma200"]) else None,
            "atr": float(row["atr14"]) if pd.notna(row["atr14"]) else None,
            "hi20": float(row["hi20_prior"]) if pd.notna(row.get("hi20_prior")) else None,
            "lo10": float(row["lo10_prior"]) if pd.notna(row.get("lo10_prior")) else None,
        },
    }


# --------------------------------------------------------------------------
# Day-trade view (5-minute bars)
# --------------------------------------------------------------------------

def _day_view(df: pd.DataFrame, bars: pd.DataFrame) -> dict:
    if bars.empty or len(bars) < 4:
        return {"score": 0, "verdict": "No session data",
                "reasons": ["Market closed or no intraday bars yet; day view resumes at the next open."]}

    pts: list[tuple[int, str]] = []

    def add(p: int, why: str):
        pts.append((p, why))

    c = float(bars["Close"].iloc[-1])
    vwap = _vwap(bars)
    v_now = float(vwap.iloc[-1])

    add(10 if c > v_now else -10,
        f"Price {'above' if c > v_now else 'below'} VWAP ({v_now:.2f})")
    if len(vwap) > 6:
        add(5 if v_now > float(vwap.iloc[-7]) else -5,
            f"VWAP sloping {'up' if v_now > float(vwap.iloc[-7]) else 'down'}")

    if len(bars) >= 3:
        orh = float(bars["High"].iloc[:3].max())
        orl = float(bars["Low"].iloc[:3].min())
        if c > orh:
            add(10, f"Above the 15-min opening range high ({orh:.2f})")
        elif c < orl:
            add(-10, f"Below the 15-min opening range low ({orl:.2f})")
        else:
            add(0, f"Inside the opening range ({orl:.2f}-{orh:.2f}): no range signal yet")

    if len(bars) >= 6:
        o = float(bars["Open"].iloc[0])
        c30 = float(bars["Close"].iloc[5])
        move30 = (c30 - o) / o
        if abs(move30) >= 0.0015:
            add(5 if move30 > 0 else -5,
                f"First 30 minutes {'up' if move30 > 0 else 'down'} {abs(move30)*100:.2f}% "
                "(early direction tends to persist)")

    prev_close = float(df["Close"].iloc[-2]) if len(df) >= 2 else None
    if prev_close:
        o = float(bars["Open"].iloc[0])
        gap = (o - prev_close) / prev_close
        if abs(gap) >= 0.003:
            holding = c >= o if gap > 0 else c <= o
            if holding:
                add(5 if gap > 0 else -5,
                    f"Gapped {'up' if gap > 0 else 'down'} {abs(gap)*100:.1f}% and holding "
                    f"{'above' if gap > 0 else 'below'} the open")
            else:
                add(-8 if gap > 0 else 8,
                    f"Gap {'up' if gap > 0 else 'down'} {abs(gap)*100:.1f}% is FADING "
                    "(gap-fill move under way)")

    rsi_i = _rsi_series(bars["Close"], 14)
    if pd.notna(rsi_i.iloc[-1]):
        r = float(rsi_i.iloc[-1])
        if r >= 70:
            add(-6, f"Intraday RSI {r:.0f} overbought")
        elif r <= 30:
            add(6, f"Intraday RSI {r:.0f} oversold")

    # Volume pace must compare consolidated volume with the consolidated
    # 20-day average. The daily bar's (partial) volume is consolidated;
    # Alpaca IEX bar volume is exchange-only and would look ~30x too small.
    avg_vol = df["avg_vol20"].iloc[-1] if "avg_vol20" in df else np.nan
    today_vol = float(df["Volume"].iloc[-1]) if str(df.index[-1].date()) == datetime.now(NY).strftime("%Y-%m-%d") else None
    if pd.notna(avg_vol) and avg_vol > 0 and today_vol:
        elapsed = max(len(bars) / 78.0, 1 / 78.0)   # 78 5-min bars per session
        pace = today_vol / (float(avg_vol) * min(elapsed, 1.0))
        if pace > 1.3:
            up = c >= float(bars["Open"].iloc[0])
            add(5 if up else -5, f"Volume running {pace:.1f}x normal pace on a{'n up' if up else ' down'} session")

    recent = bars.iloc[-6:]
    if float(recent["High"].max()) >= float(bars["High"].max()):
        add(5, "Making new session highs in the last 30 minutes")
    elif float(recent["Low"].min()) <= float(bars["Low"].min()):
        add(-5, "Making new session lows in the last 30 minutes")

    score = sum(p for p, _ in pts)
    return {
        "score": int(score),
        "verdict": _verdict(score),
        "reasons": [f"{'+' if p > 0 else ''}{p} {why}" for p, why in
                    sorted(pts, key=lambda t: -abs(t[0]))],
        "vwap": v_now,
        "last": c,
    }


# --------------------------------------------------------------------------
# Price projections (statistical cones)
# --------------------------------------------------------------------------

def _predict(df: pd.DataFrame, bars: pd.DataFrame, now: datetime) -> dict:
    close = df["Close"]
    last = float(bars["Close"].iloc[-1]) if not bars.empty else float(close.iloc[-1])
    rets = close.pct_change().dropna()
    sigma = float(rets.iloc[-20:].std()) if len(rets) >= 20 else float(rets.std())
    drift = float(rets.iloc[-20:].mean()) if len(rets) >= 20 else 0.0

    session_open = now.weekday() < 5 and "09:30" <= now.strftime("%H:%M") < "16:00"

    # --- end of day
    if session_open and len(bars) >= 6:
        done = min(len(bars) / 78.0, 1.0)
        remain = max(1.0 - done, 0.0)
        # drift: OLS slope over the last 2 hours, projected over remaining bars
        tail = bars["Close"].iloc[-24:]
        x = np.arange(len(tail), dtype=float)
        slope = float(np.polyfit(x, tail.to_numpy(dtype=float), 1)[0]) if len(tail) >= 6 else 0.0
        point = last + slope * remain * 78
        band = last * sigma * np.sqrt(remain)
        point = float(np.clip(point, last - band, last + band))
        eod = {"point": point, "low": last - band, "high": last + band,
               "label": "today's close"}
    else:
        band = last * sigma
        eod = {"point": last * (1 + drift), "low": last - band, "high": last + band,
               "label": "next session's close"}

    # --- end of week (Friday close)
    days_left = max(4 - now.weekday(), 0)  # Mon->4 .. Fri->0
    if session_open:
        h = days_left + max(1.0 - min(len(bars) / 78.0, 1.0), 0.0)
    elif now.weekday() >= 5 or (now.weekday() == 4 and now.strftime("%H:%M") >= "16:00"):
        h, days_left = 5.0, 5  # weekend / Friday evening: project next Friday
    else:
        h = float(days_left) if days_left else 1.0
    band_w = last * sigma * np.sqrt(max(h, 0.25))
    eow = {"point": last * (1 + drift * h), "low": last - band_w, "high": last + band_w,
           "label": "Friday's close" if days_left <= 4 else "next Friday's close"}

    return {
        "last": last,
        "eod": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in eod.items()},
        "eow": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in eow.items()},
        "basis": (f"20-day realized volatility {sigma*100:.2f}%/day, drift {drift*100:+.2f}%/day. "
                  "Bands are +/-1 sigma scaled by the remaining horizon: expect the close inside "
                  "the range ~2 times out of 3. Statistical cone, not a forecast of direction."),
    }


# --------------------------------------------------------------------------
# Reversal detection (shared with the alert monitor)
# --------------------------------------------------------------------------

def reversal_flags(df: pd.DataFrame, bars: pd.DataFrame) -> list[dict]:
    """Reversal evidence on the daily chart and today's session.

    Returns [{direction: 'bullish'|'bearish', what: str}, ...]
    """
    out: list[dict] = []
    if len(df) < 30:
        return out
    rsi = df["rsi14"]
    close = df["Close"]

    # RSI crossing back through the bands
    if pd.notna(rsi.iloc[-1]) and pd.notna(rsi.iloc[-2]):
        if rsi.iloc[-2] < 30 <= rsi.iloc[-1]:
            out.append({"direction": "bullish", "what": f"RSI(14) crossed back above 30 ({rsi.iloc[-1]:.0f})"})
        if rsi.iloc[-2] > 70 >= rsi.iloc[-1]:
            out.append({"direction": "bearish", "what": f"RSI(14) dropped back below 70 ({rsi.iloc[-1]:.0f})"})

    # MACD histogram inflection after a run
    hist = (df["macd"] - df["macd_signal"]).iloc[-4:]
    if len(hist) == 4 and hist.notna().all():
        h = hist.to_numpy()
        if h[-1] > h[-2] and h[-2] <= h[-3] <= h[-4] and h[-2] < 0:
            out.append({"direction": "bullish", "what": "MACD histogram turning up from a trough"})
        if h[-1] < h[-2] and h[-2] >= h[-3] >= h[-4] and h[-2] > 0:
            out.append({"direction": "bearish", "what": "MACD histogram rolling over from a peak"})

    # 20-day price/RSI divergence
    if len(df) >= 21 and rsi.iloc[-21:].notna().all():
        win_c, win_r = close.iloc[-21:], rsi.iloc[-21:]
        if win_c.iloc[-1] <= win_c.min() * 1.002 and float(win_r.iloc[-1]) > float(win_r.min()) + 5:
            out.append({"direction": "bullish", "what": "Bullish divergence: new 20-day price low but RSI holding higher"})
        if win_c.iloc[-1] >= win_c.max() * 0.998 and float(win_r.iloc[-1]) < float(win_r.max()) - 5:
            out.append({"direction": "bearish", "what": "Bearish divergence: new 20-day price high but RSI fading"})

    # Reversal candle after a 3-session run
    o, c, hi, lo = (df[k].iloc[-1] for k in ("Open", "Close", "High", "Low"))
    rng = hi - lo
    threedown = (close.diff().iloc[-4:-1] < 0).all()
    threeup = (close.diff().iloc[-4:-1] > 0).all()
    if rng > 0:
        if threedown and c > o and (c - lo) / rng > 0.7:
            out.append({"direction": "bullish", "what": "Strong reversal candle (close near the high) after 3 down sessions"})
        if threeup and c < o and (hi - c) / rng > 0.7:
            out.append({"direction": "bearish", "what": "Weak reversal candle (close near the low) after 3 up sessions"})

    # Intraday VWAP reclaim / loss
    if not bars.empty and len(bars) >= 12:
        vwap = _vwap(bars)
        dev = bars["Close"] / vwap - 1.0
        if float(dev.iloc[-1]) > 0 and float(dev.iloc[:-1].min()) < -0.005:
            out.append({"direction": "bullish", "what": "Reclaimed VWAP after trading >0.5% below it today"})
        if float(dev.iloc[-1]) < 0 and float(dev.iloc[:-1].max()) > 0.005:
            out.append({"direction": "bearish", "what": "Lost VWAP after trading >0.5% above it today"})

    return out


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def analyze(tickers: list[str]) -> list[dict]:
    now = datetime.now(NY)
    results = []
    for t in tickers:
        df = load_history(t, days=460)
        if df.empty or len(df) < 60:
            results.append({"ticker": t, "error": "no or insufficient price data"})
            continue
        bars = _intraday(t)
        rev = reversal_flags(df, bars)
        pred = _predict(df, bars, now)
        # Real-time last trade from Alpaca when available (yfinance lags 1-2 min)
        live = marketdata.latest_price(t)
        if live:
            pred["last"] = round(live, 2)
        results.append({
            "ticker": t,
            "swing": _swing_view(df),
            "day": _day_view(df, bars),
            "prediction": pred,
            "reversals": rev,
            "data_source": bars.attrs.get("source", "yfinance") if not bars.empty else "yfinance",
        })
    return results


# --------------------------------------------------------------------------
# Chart engine: multi-timeframe candles + TRAMA + levels + trends + RSI/BB
# --------------------------------------------------------------------------

def _trama(df: pd.DataFrame, length: int = 34) -> pd.Series:
    """Trend Regularity Adaptive Moving Average (LuxAlgo port).

    The smoothing factor is the squared SMA of "did the rolling high/low
    just make a new extreme": flat when the range is quiet, fast when the
    trend is regular. ama[i] = ama[i-1] + tc[i]^2 * (close[i] - ama[i-1]).
    """
    close = df["Close"].to_numpy(dtype=float)
    hh = df["High"].rolling(length).max()
    ll = df["Low"].rolling(length).min()
    new_hi = (hh.diff() > 0)
    new_lo = (ll.diff() < 0)
    tc = ((new_hi | new_lo).astype(float).rolling(length).mean() ** 2).to_numpy()
    out = np.full(len(close), np.nan)
    prev = None
    for i in range(len(close)):
        if np.isnan(tc[i]):
            continue
        prev = close[i] if prev is None else prev + tc[i] * (close[i] - prev)
        out[i] = prev
    return pd.Series(out, index=df.index)


def _pivot_indices(vals: np.ndarray, k: int = 3, mode: str = "high") -> list[int]:
    idx = []
    for i in range(k, len(vals) - k):
        win = vals[i - k:i + k + 1]
        if mode == "high" and vals[i] >= win.max():
            idx.append(i)
        elif mode == "low" and vals[i] <= win.min():
            idx.append(i)
    # collapse plateaus (consecutive indices) to their last bar
    out = []
    for i in idx:
        if out and i - out[-1] <= k:
            out[-1] = i
        else:
            out.append(i)
    return out


def _trendlines(df: pd.DataFrame, times: list) -> list[dict]:
    """Support/resistance trendlines through the last two pivot lows/highs,
    extended to the latest bar."""
    out = []
    n = len(df)
    lo, hi = df["Low"].to_numpy(float), df["High"].to_numpy(float)
    for mode, vals, name in (("high", hi, "resistance"), ("low", lo, "support")):
        piv = [i for i in _pivot_indices(vals, 3, mode) if i >= n - 120]
        if len(piv) < 2:
            continue
        i1, i2 = piv[-2], piv[-1]
        if i2 <= i1:
            continue
        slope = (vals[i2] - vals[i1]) / (i2 - i1)
        end_val = vals[i1] + slope * (n - 1 - i1)
        direction = "falling" if slope < 0 else "rising" if slope > 0 else "flat"
        out.append({
            "label": f"{direction} {name}",
            "points": [
                {"time": times[i1], "value": round(float(vals[i1]), 2)},
                {"time": times[n - 1], "value": round(float(end_val), 2)},
            ],
        })
    return out


def _rsi_divergence(df: pd.DataFrame, rsi: pd.Series, times: list) -> dict | None:
    """Two-pivot price/RSI divergence within the last ~80 bars."""
    n = len(df)
    lookback = max(n - 80, 0)
    close = df["Close"].to_numpy(float)
    r = rsi.to_numpy(float)

    lows = [i for i in _pivot_indices(close, 3, "low") if i >= lookback and not np.isnan(r[i])]
    if len(lows) >= 2:
        i1, i2 = lows[-2], lows[-1]
        if close[i2] < close[i1] and r[i2] > r[i1] + 2:
            return {
                "direction": "bullish",
                "text": (f"Bullish RSI divergence: price low {close[i2]:.2f} under {close[i1]:.2f} "
                         f"while RSI rose {r[i1]:.0f} -> {r[i2]:.0f}"),
                "price": [{"time": times[i1], "value": round(float(close[i1]), 2)},
                          {"time": times[i2], "value": round(float(close[i2]), 2)}],
                "rsi": [{"time": times[i1], "value": round(float(r[i1]), 2)},
                        {"time": times[i2], "value": round(float(r[i2]), 2)}],
                "marker_time": times[i2],
            }
    highs = [i for i in _pivot_indices(close, 3, "high") if i >= lookback and not np.isnan(r[i])]
    if len(highs) >= 2:
        i1, i2 = highs[-2], highs[-1]
        if close[i2] > close[i1] and r[i2] < r[i1] - 2:
            return {
                "direction": "bearish",
                "text": (f"Bearish RSI divergence: price high {close[i2]:.2f} over {close[i1]:.2f} "
                         f"while RSI fell {r[i1]:.0f} -> {r[i2]:.0f}"),
                "price": [{"time": times[i1], "value": round(float(close[i1]), 2)},
                          {"time": times[i2], "value": round(float(close[i2]), 2)}],
                "rsi": [{"time": times[i1], "value": round(float(r[i1]), 2)},
                        {"time": times[i2], "value": round(float(r[i2]), 2)}],
                "marker_time": times[i2],
            }
    return None


def _key_levels(ticker: str) -> list[dict]:
    """Day open, previous day high/low, and this week's high/low."""
    d = load_history(ticker, days=30)
    if d.empty or len(d) < 3:
        return []
    last, prev = d.iloc[-1], d.iloc[-2]
    week_key = d.index[-1].isocalendar().week
    week = d[[ts.isocalendar().week == week_key for ts in d.index]]
    if len(week) < 2:  # Monday: this week is one bar, use the prior week
        prior_key = d.index[-2].isocalendar().week
        week = d[[ts.isocalendar().week == prior_key for ts in d.index]]
    return [
        {"key": "day_open", "label": "Day open", "price": round(float(last["Open"]), 2)},
        {"key": "pdh", "label": "Prev day high", "price": round(float(prev["High"]), 2)},
        {"key": "pdl", "label": "Prev day low", "price": round(float(prev["Low"]), 2)},
        {"key": "wk_hi", "label": "Week high", "price": round(float(week["High"].max()), 2)},
        {"key": "wk_lo", "label": "Week low", "price": round(float(week["Low"].min()), 2)},
    ]


def _resample_4h(df15: pd.DataFrame) -> pd.DataFrame:
    """Two session-anchored 4-hour candles per day (09:30-13:30, 13:30-16:00)."""
    rows = []
    for day, g in df15.groupby(df15.index.date):
        hm = g.index.strftime("%H:%M")
        for lo, hi in (("09:30", "13:30"), ("13:30", "16:00")):
            seg = g[(hm >= lo) & (hm < hi)]
            if seg.empty:
                continue
            rows.append({
                "t": seg.index[0],
                "Open": float(seg["Open"].iloc[0]),
                "High": float(seg["High"].max()),
                "Low": float(seg["Low"].min()),
                "Close": float(seg["Close"].iloc[-1]),
                "Volume": float(seg["Volume"].sum()),
            })
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows).set_index("t")
    out.attrs["source"] = df15.attrs.get("source", "yfinance")
    return out


TIMEFRAMES = ("5m", "15m", "4h", "1d")


def _bars_for(ticker: str, tf: str) -> pd.DataFrame:
    if tf == "5m":
        return marketdata.bars(ticker, "5Min", days=5, yf_symbol=ticker)
    if tf == "15m":
        return marketdata.bars(ticker, "15Min", days=20, yf_symbol=ticker)
    if tf == "4h":
        return _resample_4h(marketdata.bars(ticker, "15Min", days=140, yf_symbol=ticker))
    df = load_history(ticker, days=560)
    return df.iloc[-260:] if len(df) > 260 else df


def _times(index: pd.DatetimeIndex, tf: str) -> list:
    """lightweight-charts time values. Intraday: ET wall-clock encoded as UTC
    epoch so the chart's UTC display shows New York session times."""
    if tf == "1d":
        return [str(ts.date()) for ts in index]
    epoch = pd.Timestamp("1970-01-01")
    return [int((ts - epoch).total_seconds()) for ts in index]


def chart_data(ticker: str, tf: str = "1d") -> dict:
    if tf not in TIMEFRAMES:
        return {"error": f"timeframe must be one of {TIMEFRAMES}"}
    df = _bars_for(ticker, tf)
    if df.empty or len(df) < 40:
        return {"error": f"not enough {tf} bars for {ticker}"}
    times = _times(df.index, tf)

    candles = [
        {"time": t, "open": round(float(o), 2), "high": round(float(h), 2),
         "low": round(float(l), 2), "close": round(float(c), 2)}
        for t, o, h, l, c in zip(times, df["Open"], df["High"], df["Low"], df["Close"])
    ]
    trama = _trama(df, 34)
    rsi = _rsi_series(df["Close"], 14)
    mid = rsi.rolling(20).mean()
    sd = rsi.rolling(20).std()

    def line(series):
        return [{"time": t, "value": round(float(v), 2)}
                for t, v in zip(times, series) if pd.notna(v)]

    # Session VWAP for intraday timeframes: resets every day. The first bar
    # of each session is emitted as whitespace ({time} without a value) so
    # the chart breaks the line between sessions instead of drawing a
    # connector across the overnight gap.
    vwap_pts: list[dict] = []
    if tf in ("5m", "15m"):
        tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
        day = pd.Series(df.index.date, index=df.index)
        cum_pv = (tp * df["Volume"]).groupby(day.values).cumsum()
        cum_v = df["Volume"].groupby(day.values).cumsum().replace(0, np.nan)
        vwap = cum_pv / cum_v
        prev_day = None
        for t, ts, v in zip(times, df.index, vwap):
            if ts.date() != prev_day:
                vwap_pts.append({"time": t})   # whitespace: break between sessions
                prev_day = ts.date()
            elif pd.notna(v):
                vwap_pts.append({"time": t, "value": round(float(v), 2)})

    return {
        "ticker": ticker,
        "tf": tf,
        "source": df.attrs.get("source", "yfinance"),
        "candles": candles,
        "trama": line(trama),
        "vwap": vwap_pts,
        "rsi": line(rsi),
        "rsi_bb_mid": line(mid),
        "rsi_bb_up": line(mid + 2 * sd),
        "rsi_bb_lo": line(mid - 2 * sd),
        "levels": _key_levels(ticker),
        "trendlines": _trendlines(df, times),
        "divergence": _rsi_divergence(df, rsi, times),
    }


def scan_reversals(tickers: list[str]) -> list[dict]:
    """Fresh reversal events for the alert monitor."""
    events = []
    for t in tickers:
        df = load_history(t, days=460)
        if df.empty or len(df) < 30:
            continue
        bars = _intraday(t)
        for f in reversal_flags(df, bars):
            events.append({
                "ticker": t,
                "direction": f["direction"],
                "what": f["what"],
                "price": float(bars["Close"].iloc[-1]) if not bars.empty else float(df["Close"].iloc[-1]),
            })
    return events
