"""0DTE-style same-day strategies for SPY, SPX, QQQ, IWM.

Strategy research summary
-------------------------
The most consistently documented same-day edges on index products:

1. ORB 15-min Breakout: trade the break of the first 15 minutes, stop at
   the opposite side of the range, exit by the close (Zarattini & Aziz,
   2023, documented on QQQ).
2. First 30-min Momentum: the direction of the first half hour tends to
   persist into the close on S&P products (Gao, Han, Li & Zhou, 2018).
3. VWAP Reversion: index products are strongly mean-reverting intraday;
   fade stretched deviations from VWAP back to VWAP.
4. Gap Fade: large overnight gaps on index ETFs tend to partially fill
   during the session.
5. Expected-Move Straddle Sell (proxy): the most popular real 0DTE options
   trade is selling the expected move at the open (condors/straddles) and
   letting theta decay. Modeled here as: credit = 0.8 x 20-day realized
   1-sigma daily move; an intraday gamma-stop (via the day's high/low)
   closes the trade for a loss if the underlying runs 1.5x the credit from
   the open, otherwise it is held to the close for credit - |move|, less a
   round-trip friction either way. The stop is what makes the win rate and
   tail losses realistic -- an open-to-close-only model hides every "run
   over then recovered" day and prints a fantasy ~100% win rate.

Data limitations: free intraday data (yfinance) covers only ~60 days of
5-minute bars, so intraday strategies are clipped to that window. Gap Fade
and the straddle proxy use daily bars and work for any date range. True
0DTE options backtests require paid historical options data (e.g. CBOE
DataShop). All trades open and close the same session.
"""

from dataclasses import asdict, dataclass

import pandas as pd
import yfinance as yf

from . import marketdata

SYMBOLS = {"SPY": "SPY", "SPX": "^GSPC", "QQQ": "QQQ", "IWM": "IWM"}
INTRADAY_MAX_DAYS = 55  # yfinance 5m fallback limit (~60 days) for cash indices

GAP_THRESHOLD = 0.003        # 0.3% overnight gap to trigger a fade
VWAP_DEV_THRESHOLD = 0.003   # 0.3% stretch from VWAP to trigger reversion
MOMENTUM_MIN_MOVE = 0.0015   # 0.15% first-30-min move to trigger momentum
STRADDLE_PREMIUM_MULT = 0.8  # straddle premium ~ 0.8 x 1-sigma expected move
# A short straddle is short gamma: as the underlying runs away from the strike
# intraday the loss accelerates, so a real seller stops out well before the
# close. Model that with an intraday gamma-stop keyed off the day's high/low --
# without it the open-to-close-only PnL hides every "run over then came back"
# day and prints a fantasy ~100% win rate.
STRADDLE_STOP_MULT = 1.5     # stop when the intraday move from the open reaches 1.5x the credit (~1.2 sigma; trips ~25% of days)
STRADDLE_COST = 0.15         # round-trip options friction (spread + slippage) as a fraction of the credit


@dataclass
class DayTrade:
    strategy: str
    symbol: str
    date: str
    direction: str
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    pnl_pct: float
    stop_price: float | None = None


def _stats(trades: list[DayTrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    pnls = [t.pnl_pct for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": n,
        "win_rate": 100.0 * len(wins) / n,
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": -gross_loss / len(losses) if losses else 0.0,
        "expectancy": sum(pnls) / n,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
    }


def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        level = 0 if "Close" in df.columns.get_level_values(0) else -1
        df.columns = df.columns.get_level_values(level)
    return df.loc[:, ~df.columns.duplicated()]


def load_daily(yf_symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    try:
        df = yf.Ticker(yf_symbol).history(
            start=str((start - pd.Timedelta(days=60)).date()),  # sigma warmup
            end=str((end + pd.Timedelta(days=1)).date()),
            interval="1d",
            auto_adjust=True,
        )
    except Exception as exc:
        print(f"  ! failed to fetch daily {yf_symbol}: {exc}")
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    df = _flatten(df)
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    return df.dropna(subset=["Close"])


def load_intraday(yf_symbol: str, start: pd.Timestamp, end: pd.Timestamp):
    """5-minute bars; returns (df, clipped) where clipped means the requested
    start predates the free-data window and was moved forward."""
    today = pd.Timestamp.today().normalize()
    min_start = today - pd.Timedelta(days=INTRADAY_MAX_DAYS)
    clipped = start < min_start
    if end < min_start:
        return pd.DataFrame(), True
    s = max(start, min_start)
    try:
        df = yf.Ticker(yf_symbol).history(
            start=str(s.date()),
            end=str((end + pd.Timedelta(days=1)).date()),
            interval="5m",
            auto_adjust=True,
        )
    except Exception as exc:
        print(f"  ! failed to fetch intraday {yf_symbol}: {exc}")
        return pd.DataFrame(), clipped
    if df is None or df.empty:
        return pd.DataFrame(), clipped
    df = _flatten(df)
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_convert("America/New_York").tz_localize(None)
    return df.dropna(subset=["Close"]), clipped


# --- Intraday strategies: take one session of 5m bars (DatetimeIndex kept),
# return a dict of trade details or None ---

def _orb15(bars: pd.DataFrame):
    if len(bars) < 12:
        return None
    orh = float(bars["High"].iloc[:3].max())  # first 15 minutes = 3 x 5m bars
    orl = float(bars["Low"].iloc[:3].min())
    direction = entry = entry_i = stop = None
    for i in range(3, len(bars)):
        c = float(bars["Close"].iloc[i])
        if c > orh:
            direction, entry, entry_i, stop = 1, c, i, orl
            break
        if c < orl:
            direction, entry, entry_i, stop = -1, c, i, orh
            break
    if direction is None:
        return None
    exit_i, exit_price = len(bars) - 1, float(bars["Close"].iloc[-1])
    for j in range(entry_i + 1, len(bars)):
        if direction == 1 and float(bars["Low"].iloc[j]) <= stop:
            exit_i, exit_price = j, stop
            break
        if direction == -1 and float(bars["High"].iloc[j]) >= stop:
            exit_i, exit_price = j, stop
            break
    return {
        "direction": "long" if direction == 1 else "short",
        "entry_time": bars.index[entry_i].strftime("%H:%M"),
        "entry_price": entry,
        "exit_time": bars.index[exit_i].strftime("%H:%M"),
        "exit_price": exit_price,
        "pnl_pct": direction * (exit_price - entry) / entry * 100.0,
        "stop_price": stop,
    }


def _vwap_reversion(bars: pd.DataFrame):
    if len(bars) < 12:
        return None
    tp = (bars["High"] + bars["Low"] + bars["Close"]) / 3.0
    vol = bars["Volume"]
    if float(vol.sum()) > 0:
        vwap = (tp * vol).cumsum() / vol.cumsum()
    else:  # cash indices like ^GSPC have no volume
        vwap = tp.expanding().mean()
    dev = bars["Close"] / vwap - 1.0
    direction = entry = entry_i = None
    for i in range(6, len(bars)):  # skip the first 30 minutes
        d = float(dev.iloc[i])
        if d >= VWAP_DEV_THRESHOLD:
            direction, entry, entry_i = -1, float(bars["Close"].iloc[i]), i
            break
        if d <= -VWAP_DEV_THRESHOLD:
            direction, entry, entry_i = 1, float(bars["Close"].iloc[i]), i
            break
    if direction is None:
        return None
    exit_i, exit_price = len(bars) - 1, float(bars["Close"].iloc[-1])
    for j in range(entry_i + 1, len(bars)):
        c = float(bars["Close"].iloc[j])
        if (direction == 1 and c >= float(vwap.iloc[j])) or (
            direction == -1 and c <= float(vwap.iloc[j])
        ):
            exit_i, exit_price = j, c
            break
    return {
        "direction": "long" if direction == 1 else "short",
        "entry_time": bars.index[entry_i].strftime("%H:%M"),
        "entry_price": entry,
        "exit_time": bars.index[exit_i].strftime("%H:%M"),
        "exit_price": exit_price,
        "pnl_pct": direction * (exit_price - entry) / entry * 100.0,
    }


def _momentum30(bars: pd.DataFrame):
    if len(bars) < 20:
        return None
    first30 = float(bars["Close"].iloc[5]) / float(bars["Open"].iloc[0]) - 1.0
    if abs(first30) < MOMENTUM_MIN_MOVE:
        return None
    direction = 1 if first30 > 0 else -1
    entry = float(bars["Close"].iloc[5])
    exit_price = float(bars["Close"].iloc[-1])
    return {
        "direction": "long" if direction == 1 else "short",
        "entry_time": bars.index[5].strftime("%H:%M"),
        "entry_price": entry,
        "exit_time": bars.index[-1].strftime("%H:%M"),
        "exit_price": exit_price,
        "pnl_pct": direction * (exit_price - entry) / entry * 100.0,
    }


INTRADAY = {
    "orb15": ("ORB 15-min Breakout", _orb15),
    "vwap": ("VWAP Reversion", _vwap_reversion),
    "momentum30": ("First 30-min Momentum", _momentum30),
}
DAILY_LABELS = {
    "gapfade": "Gap Fade",
    "straddle": "Expected-Move Straddle Sell (proxy)",
}
STRATEGY_LABELS = {**{k: v[0] for k, v in INTRADAY.items()}, **DAILY_LABELS}


def _gap_fade(daily: pd.DataFrame, start, end, symbol: str) -> list[DayTrade]:
    trades = []
    prev_close = daily["Close"].shift(1)
    gap = daily["Open"] / prev_close - 1.0
    for ts in daily.index:
        if ts < start or ts > end:
            continue
        g = gap.loc[ts]
        if pd.isna(g) or abs(g) < GAP_THRESHOLD:
            continue
        direction = -1 if g > 0 else 1
        o, c = float(daily["Open"].loc[ts]), float(daily["Close"].loc[ts])
        pnl = direction * (c - o) / o * 100.0
        trades.append(
            DayTrade(
                strategy=DAILY_LABELS["gapfade"],
                symbol=symbol,
                date=str(ts.date()),
                direction="short" if direction == -1 else "long",
                entry_time="09:30 (open)",
                entry_price=o,
                exit_time="16:00 (close)",
                exit_price=c,
                pnl_pct=pnl,
            )
        )
    return trades


def _straddle_sell(daily: pd.DataFrame, start, end, symbol: str) -> list[DayTrade]:
    """Short ATM straddle at the open, theta-decay to the close -- with an
    intraday gamma-stop so getting run over midday costs real money.

    Per day: collect a credit ~= 0.8 x the 1-sigma expected move. The
    breakeven distance from the open equals that credit. Using the day's
    high/low, take the worst one-sided excursion from the open (MAE); if it
    reaches STRADDLE_STOP_MULT x the credit distance, the position is stopped
    out intraday for a loss -- regardless of where it closes (the key fix:
    the old model only saw open-to-close and never registered these). Days
    that never hit the stop are held to the close for premium - |move|. A
    round-trip friction (spread + slippage) is charged either way.
    """
    trades = []
    sigma = daily["Close"].pct_change().rolling(20).std().shift(1)  # known at open
    for ts in daily.index:
        if ts < start or ts > end:
            continue
        s = sigma.loc[ts]
        if pd.isna(s):
            continue
        o = float(daily["Open"].loc[ts])
        h = float(daily["High"].loc[ts])
        lo = float(daily["Low"].loc[ts])
        c = float(daily["Close"].loc[ts])

        premium = STRADDLE_PREMIUM_MULT * s          # credit = breakeven distance
        stop_move = STRADDLE_STOP_MULT * premium     # excursion that trips the stop
        up, dn = h / o - 1.0, 1.0 - lo / o
        mae = max(up, dn)                            # worst one-sided move from the open

        if mae >= stop_move:
            # Short gamma stop: intrinsic loss beyond the credit, plus friction.
            loss = (stop_move - premium) + premium * STRADDLE_COST
            pnl = float(-loss * 100.0)
            stop_price = o * (1.0 + stop_move) if up >= dn else o * (1.0 - stop_move)
            exit_time, exit_price = "intraday (gamma stop)", stop_price
        else:
            move = abs(c / o - 1.0)
            net_credit = premium * (1.0 - STRADDLE_COST)   # spread eats part of the credit
            pnl = float((net_credit - move) * 100.0)
            stop_price = o * (1.0 + stop_move)
            exit_time, exit_price = "16:00 (close)", c

        trades.append(
            DayTrade(
                strategy=DAILY_LABELS["straddle"],
                symbol=symbol,
                date=str(ts.date()),
                direction="short_vol",
                entry_time="09:30 (open)",
                entry_price=o,
                exit_time=exit_time,
                exit_price=round(exit_price, 2),
                pnl_pct=pnl,
                stop_price=round(stop_price, 2),
            )
        )
    return trades


def _intraday_range(yf_sym: str, start: pd.Timestamp, end: pd.Timestamp):
    """5-minute session bars over [start, end], regular hours only.

    Uses Alpaca IEX -- the SAME real-time feed the live monitor trades on, with
    years of history -- via the read-only market-data endpoint (never the paper
    trading account). Cash indices like ^GSPC aren't on Alpaca, so those fall
    back to yfinance automatically (still ~60-day limited). Returns
    (df, source, clipped).
    """
    today = pd.Timestamp.today().normalize()
    span = max((today - start).days + 2, 1)
    df = marketdata.bars(yf_sym, "5Min", days=span)
    source = df.attrs.get("source", "?") if not df.empty else "none"
    clipped = source != "alpaca" and start < (today - pd.Timedelta(days=INTRADAY_MAX_DAYS))
    if df.empty:
        return df, source, clipped
    df = df[(df.index >= start) & (df.index < end + pd.Timedelta(days=1))]
    return df, source, clipped


def run(symbols: list[str], strategies: list[str], start=None, end=None) -> dict:
    """Backtest the selected 0DTE strategies per symbol over [start, end].

    Intraday strategies (ORB / VWAP / Momentum) are replayed with the EXACT
    evaluators the live monitor uses (``alerts._eval_intraday`` -- VWAP and ORB
    re-enter after each exit; open positions flatten at the session close), on
    Alpaca bars, so a completed session's backtest matches what the monitor
    logged live. Daily strategies (gap fade / straddle) use daily bars.
    """
    end = pd.Timestamp(end) if end else pd.Timestamp.today().normalize()
    start = pd.Timestamp(start) if start else end - pd.Timedelta(days=30)
    if start > end:
        raise ValueError("start date is after end date")
    strategies = [s for s in strategies if s in STRATEGY_LABELS] or list(STRATEGY_LABELS)
    need_intraday = [s for s in strategies if s in INTRADAY]
    need_daily = [s for s in strategies if s in DAILY_LABELS]

    # Same evaluators the live monitor runs (re-entry + EOD flatten). Imported
    # lazily: alerts imports this module, so a top-level import would cycle.
    from . import alerts

    rows, notes = [], set()
    for sym in symbols:
        yf_sym = SYMBOLS.get(sym.upper())
        if not yf_sym:
            continue
        trades_by: dict[str, list[DayTrade]] = {name: [] for name in strategies}

        if need_intraday:
            bars, source, clipped = _intraday_range(yf_sym, start, end)
            if clipped:
                notes.add(
                    f"{sym}: intraday history from {source} only reaches ~{INTRADAY_MAX_DAYS} "
                    "days back (cash indices have no Alpaca feed); older dates were clipped."
                )
            if not bars.empty:
                for date, day in bars.groupby(bars.index.date):
                    for name in need_intraday:
                        # session_over=True -> anything still open exits at the
                        # session's last bar, modelling the 16:00 flatten
                        for res in alerts._eval_intraday(name, day, session_over=True):
                            trades_by[name].append(DayTrade(
                                strategy=STRATEGY_LABELS[name], symbol=sym, date=str(date),
                                direction=res["direction"], entry_time=res["entry_time"],
                                entry_price=res["entry_price"], exit_time=res["exit_time"],
                                exit_price=res["exit_price"], pnl_pct=res["pnl_pct"],
                                stop_price=res.get("stop_price"),
                            ))

        if need_daily:
            daily = load_daily(yf_sym, start, end)
            if not daily.empty:
                if "gapfade" in need_daily:
                    trades_by["gapfade"].extend(_gap_fade(daily, start, end, sym))
                if "straddle" in need_daily:
                    trades_by["straddle"].extend(_straddle_sell(daily, start, end, sym))

        for name in strategies:
            s = _stats(trades_by[name])
            s["strategy"] = STRATEGY_LABELS[name]
            s["name"] = name
            s["symbol"] = sym
            s["trades_detail"] = [asdict(t) for t in trades_by[name]]
            rows.append(s)

    rows.sort(key=lambda r: r.get("expectancy", float("-inf")), reverse=True)
    return {
        "rows": rows,
        "notes": sorted(notes),
        "start": str(start.date()),
        "end": str(end.date()),
    }
