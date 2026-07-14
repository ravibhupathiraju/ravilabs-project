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


def chart_data(ticker: str) -> dict:
    """Series for the two charts: 130 daily bars + today's 5-minute bars."""
    df = load_history(ticker, days=420)
    if df.empty:
        return {"error": "no data"}
    tail = df.iloc[-130:]
    daily = {
        "dates": [str(d.date()) for d in tail.index],
        "close": [round(float(v), 2) for v in tail["Close"]],
        "sma20": [round(float(v), 2) if pd.notna(v) else None for v in tail["sma20"]],
        "sma50": [round(float(v), 2) if pd.notna(v) else None for v in tail["sma50"]],
        "sma200": [round(float(v), 2) if pd.notna(v) else None for v in tail["sma200"]],
        "bb_upper": [round(float(v), 2) if pd.notna(v) else None for v in tail["bb_upper"]],
        "bb_lower": [round(float(v), 2) if pd.notna(v) else None for v in tail["bb_lower"]],
    }
    bars = _intraday(ticker)
    intraday = None
    if not bars.empty:
        vwap = _vwap(bars)
        intraday = {
            "times": [d.strftime("%H:%M") for d in bars.index],
            "close": [round(float(v), 2) for v in bars["Close"]],
            "vwap": [round(float(v), 2) for v in vwap],
        }
    return {"ticker": ticker, "daily": daily, "intraday": intraday}


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
