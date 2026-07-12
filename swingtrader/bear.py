"""Bear strategy — faithful port of the TradeLab "CRWV" swing model.

A 0-100 scored pullback-in-uptrend system (see indicators._bear_columns for
the per-ticker components). The SPY market regime adds up to 20 points:
    +10 bull market   (SPY close > EMA200)
    +5  above EMA50   (SPY close > EMA50)
    +5  EMA alignment (SPY EMA50 > EMA200)

Levels, exactly as TradeLab: entry = scan-day close, stop = close - 1.5*ATR,
target = close + 3*ATR. Confidence: >=85 Very High, >=70 High, >=55 Medium.

Research methodology, exactly as TradeLab research.py: scan a date, take the
TOP N (20) by score, hold 10 trading days; report per-date average return,
win %, target-hit % and stop-hit %, on a weekly cadence over a range.
(One deliberate fix: TradeLab evaluates the market regime with CURRENT SPY
data for every historical date; here the regime is computed as of each scan
date. Ranking is unaffected since the bonus is the same for all tickers.)
"""

from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from .data import load_history

TOP_N = 20
HOLD_DAYS = 10
MIN_HISTORY = 250
STOP_ATR = 1.5
TARGET_ATR = 3.0


# --------------------------------------------------------------------------
# Market regime (SPY)
# --------------------------------------------------------------------------

def market_regime(as_of: pd.Timestamp | None = None) -> dict:
    df = load_history("SPY", days=730)
    if as_of is not None:
        df = df[df.index <= as_of]
    if df.empty:
        return {"bull_market": False, "above_ema50": False, "ema_alignment": False,
                "close": None, "score": 0}
    close = df["Close"]
    ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
    ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])
    c = float(close.iloc[-1])
    regime = {
        "bull_market": c > ema200,
        "above_ema50": c > ema50,
        "ema_alignment": ema50 > ema200,
        "close": c,
    }
    regime["score"] = (
        10 * regime["bull_market"] + 5 * regime["above_ema50"] + 5 * regime["ema_alignment"]
    )
    return regime


# --------------------------------------------------------------------------
# Scoring (mirrors TradeLab crwv_strategy.evaluate)
# --------------------------------------------------------------------------

def _confidence(score: int) -> str:
    if score >= 85:
        return "Very High"
    if score >= 70:
        return "High"
    if score >= 55:
        return "Medium"
    return "Low"


def score_row(row: pd.Series, market: dict | None) -> dict | None:
    if pd.isna(row.get("bear_score")) or pd.isna(row.get("atr14")):
        return None
    reasons = []
    if row["trend_up"]:
        reasons.append("Strong Uptrend")
        reasons.append("EMA Alignment")
    if row["ema21_rising"]:
        reasons.append("EMA21 Rising")
    if 10 <= row["pullback_pct"] <= 30:
        reasons.append(f"Healthy Pullback ({row['pullback_pct']:.1f}%)")
    if 5 <= row["bars_since_high"] <= 25:
        reasons.append(f"{int(row['bars_since_high'])} Bars Since High")
    if row["rsi14"] < 45:
        reasons.append(f"RSI {row['rsi14']:.1f}")
    if row["rel_vol20"] > 1.20:
        reasons.append(f"Relative Volume {row['rel_vol20']:.2f}x")

    score = int(row["bear_score"])
    if market:
        score += market["score"]
        if market["bull_market"]:
            reasons.append("Bull Market")
    score = min(score, 100)

    close = float(row["Close"])
    atr = float(row["atr14"])
    return {
        "score": score,
        "confidence": _confidence(score),
        "entry": close,
        "stop": close - STOP_ATR * atr,
        "target": close + TARGET_ATR * atr,
        "reasons": reasons,
    }


# --------------------------------------------------------------------------
# Ranked scan
# --------------------------------------------------------------------------

def _rank(loaded: dict[str, pd.DataFrame], market: dict,
          as_of: pd.Timestamp | None) -> list[dict]:
    results = []
    for ticker, full in loaded.items():
        df = full if as_of is None else full[full.index <= as_of]
        if len(df) < MIN_HISTORY:
            continue
        scored = score_row(df.iloc[-1], market)
        if scored is None:
            continue
        scored["ticker"] = ticker
        scored["date"] = str(df.index[-1].date())
        results.append(scored)
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def _load_all(tickers: list[str], days: int, max_workers: int = 8) -> dict[str, pd.DataFrame]:
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        pairs = pool.map(lambda t: (t, load_history(t, days=days)), tickers)
    return {t: df for t, df in pairs if not df.empty}


def scan_rank(tickers: list[str], top: int = TOP_N, min_score: int = 0) -> dict:
    """Today's ranked candidates (TradeLab scan of the latest bar)."""
    market = market_regime()
    loaded = _load_all(tickers, days=560)
    results = [r for r in _rank(loaded, market, as_of=None) if r["score"] >= min_score]
    if top:
        results = results[:top]
    return {"market": market, "results": results, "tickers_used": len(loaded)}


# --------------------------------------------------------------------------
# Research: weekly scans -> top N -> 10-day forward evaluation
# --------------------------------------------------------------------------

def _evaluate(future: pd.DataFrame, entry: float, stop: float, target: float,
              hold_days: int = HOLD_DAYS) -> dict | None:
    future = future.head(hold_days)
    if len(future) < hold_days:
        return None  # not enough forward data yet
    highest = float(future["High"].max())
    lowest = float(future["Low"].min())
    final_close = float(future.iloc[-1]["Close"])
    return {
        "return_pct": (final_close - entry) / entry * 100.0,
        "max_gain_pct": (highest - entry) / entry * 100.0,
        "max_loss_pct": (lowest - entry) / entry * 100.0,
        "target_hit": highest >= target,
        "stop_hit": lowest <= stop,
    }


def _cohort_stats(rows: list[dict]) -> dict:
    n = len(rows)
    return {
        "picks": n,
        "avg_return": sum(r["return_pct"] for r in rows) / n,
        "win_rate": 100.0 * sum(r["return_pct"] > 0 for r in rows) / n,
        "target_hit": 100.0 * sum(r["target_hit"] for r in rows) / n,
        "stop_hit": 100.0 * sum(r["stop_hit"] for r in rows) / n,
    }


_TIER_ORDER = ["Very High", "High", "Medium", "Low"]


def research(tickers: list[str], start: str, end: str, top: int = TOP_N,
             min_score: int = 0) -> dict:
    """TradeLab multi-date research: weekly scans between start and end.

    top=0 means "no cap" (everything above min_score). Each date carries a
    per-score-tier breakdown and the individual evaluated picks so the UI
    can show how the different score levels actually performed.
    """
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    if start_ts > end_ts:
        raise ValueError("start date is after end date")
    span_days = (pd.Timestamp.today() - start_ts).days
    loaded = _load_all(tickers, days=span_days + 560)

    dates = []
    current = start_ts
    while current <= end_ts:
        market = market_regime(as_of=current)
        ranked = [r for r in _rank(loaded, market, as_of=current) if r["score"] >= min_score]
        if top:
            ranked = ranked[:top]
        evaluated = []
        for r in ranked:
            future = loaded[r["ticker"]][loaded[r["ticker"]].index > current]
            stats = _evaluate(future, r["entry"], r["stop"], r["target"])
            if stats is not None:
                evaluated.append({**r, **stats})
        if evaluated:
            by_score = [
                {"tier": tier, **_cohort_stats(group)}
                for tier in _TIER_ORDER
                if (group := [r for r in evaluated if r["confidence"] == tier])
            ]
            dates.append({
                "date": str(current.date()),
                **_cohort_stats(evaluated),
                "by_score": by_score,
                "detail": [
                    {k: r[k] for k in ("ticker", "score", "confidence", "entry",
                                       "stop", "target", "return_pct",
                                       "max_gain_pct", "max_loss_pct",
                                       "target_hit", "stop_hit")}
                    for r in evaluated
                ],
                "market": {k: market[k] for k in ("bull_market", "above_ema50", "ema_alignment")},
            })
        current += pd.Timedelta(days=7)

    summary = None
    if dates:
        summary = {
            "avg_return": sum(d["avg_return"] for d in dates) / len(dates),
            "avg_win_rate": sum(d["win_rate"] for d in dates) / len(dates),
        }
        # Aggregate tier performance across every date, for the summary line.
        all_rows = [r for d in dates for r in d["detail"]]
        summary["by_score"] = [
            {"tier": tier, **_cohort_stats(group)}
            for tier in _TIER_ORDER
            if (group := [r for r in all_rows if r["confidence"] == tier])
        ]
    return {"dates": dates, "summary": summary, "tickers_used": len(loaded)}
