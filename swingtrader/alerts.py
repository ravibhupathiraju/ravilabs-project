"""Live 0DTE alert monitor.

Runs the same five 0DTE strategies from ``zerodte.py`` against today's data
while the market is open, fires Windows desktop (toast) notifications on
entries and exits, and records every alert to a local SQLite database so the
web UI can show alert history with entry/exit prices and P/L.

Alert lifecycle per (date, symbol, strategy, seq):
  entry signal seen  -> row inserted with status 'open'  + ENTRY toast
  exit detected      -> row updated with exit/pnl, status 'closed' + EXIT toast
  session ends       -> any still-open trades are closed at the last price

Unlike the backtest (one trade per strategy per day), the live scan keeps
watching after a trade exits: ORB re-breaks and repeat VWAP stretches fire
fresh alerts the same day, numbered by ``seq``. Momentum-30, Gap Fade and
the straddle proxy have a single decision point per session by nature.

Data comes from yfinance, which lags real time by roughly a minute or two --
treat alert prices as approximate and confirm on your own quotes.
"""

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from . import analyzer, broker, marketdata, patterns, screener, zerodte
from .data import load_history
from .earnings import earnings_dates, near_earnings
from .strategies import STRATEGIES as SWING_STRATEGIES
from .universe import get_universe
from .zerodte import (
    GAP_THRESHOLD,
    INTRADAY,
    STRADDLE_PREMIUM_MULT,
    STRATEGY_LABELS,
    SYMBOLS,
    VWAP_DEV_THRESHOLD,
    _flatten,
)

DB_PATH = Path(__file__).resolve().parent.parent / "alerts.db"
NY = ZoneInfo("America/New_York")
POLL_SECONDS = 60
SWING_POLL_SECONDS = 900  # swing signals form on daily bars; 15 min is plenty
PATTERN_POLL_SECONDS = 300  # intraday pattern conditions, checked every 5 min
SESSION_END = "16:00"

PLANNED_EXITS = {
    "orb15": "stop or 16:00 close",
    "vwap": "VWAP touch or 16:00 close",
    "momentum30": "16:00 close",
    "gapfade": "16:00 close",
    "straddle": "16:00 close (theta decay)",
}

# strategy key -> label across all alert kinds
ALL_LABELS = {
    **STRATEGY_LABELS,
    **{k: s.label for k, s in SWING_STRATEGIES.items()},
    "rev_bull": "Bullish Reversal",
    "rev_bear": "Bearish Reversal",
}


def _swing_planned_exit(s) -> str:
    return s.planned_exit()


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

_COLUMNS = [
    "kind", "alert_time", "date", "symbol", "strategy", "seq", "direction",
    "entry_time", "entry_price", "stop_price", "target_price", "atr_entry",
    "planned_exit", "exit_time", "exit_price", "pnl_pct", "exit_reason", "status",
    "broker_order_id", "broker_qty",
]

_SCHEMA = """CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT '0dte', -- '0dte' or 'swing'
    alert_time TEXT NOT NULL,      -- when the alert fired (ET)
    date TEXT NOT NULL,            -- session/signal date YYYY-MM-DD
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,        -- strategy key, e.g. 'orb15' or 'rsi2'
    seq INTEGER NOT NULL DEFAULT 0,-- nth trade of the day for this combo
    direction TEXT NOT NULL,
    entry_time TEXT,               -- HH:MM for 0DTE, entry date for swing
    entry_price REAL,              -- null while a swing entry awaits its fill
    stop_price REAL,
    target_price REAL,             -- swing ATR profit target, when the strategy has one
    atr_entry REAL,                -- ATR(14) at the swing signal, fixes the stop at fill
    planned_exit TEXT,
    exit_time TEXT,                -- HH:MM for 0DTE, exit date for swing
    exit_price REAL,
    pnl_pct REAL,
    exit_reason TEXT,              -- swing: stop/profit_target/signal/time_stop/pre_earnings
    status TEXT NOT NULL DEFAULT 'open',  -- swing: signal -> open -> closed
    broker_order_id TEXT,          -- Alpaca paper order id when mirrored to the broker
    broker_qty INTEGER,            -- shares actually ordered at the broker
    UNIQUE(date, symbol, strategy, seq)
)"""


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute(_SCHEMA)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(alerts)")}
    if set(_COLUMNS) <= cols:
        return con

    # Older schema: add the missing columns in place. ALTER TABLE ADD COLUMN
    # is atomic and keeps every row, unlike a rename-copy-drop rebuild (which
    # could strand data in an `alerts_old` table if it died mid-way).
    types = {"seq": "INTEGER", "broker_qty": "INTEGER"}
    for col in _COLUMNS:
        if col not in cols:
            con.execute(
                f"ALTER TABLE alerts ADD COLUMN {col} {types.get(col, 'REAL' if col in ('entry_price', 'stop_price', 'target_price', 'atr_entry', 'exit_price', 'pnl_pct') else 'TEXT')}"
            )
    con.commit()

    # Recover rows stranded by an interrupted pre-1.1 rebuild, if any.
    stranded = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='alerts_old'"
    ).fetchone()
    if stranded:
        old_cols = {r["name"] for r in con.execute("PRAGMA table_info(alerts_old)")}
        keep = ", ".join(c for c in _COLUMNS if c in old_cols)
        con.execute(
            f"INSERT OR IGNORE INTO alerts ({keep}) SELECT {keep} FROM alerts_old"
        )
        con.execute("DROP TABLE alerts_old")
        con.commit()
        print("[alerts] recovered rows from an interrupted schema migration")
    return con


def history(
    start: str | None,
    end: str | None,
    symbols: list[str] | None,
    kind: str | None = None,
    strategy: str | None = None,
) -> list[dict]:
    """Alert rows filtered by date range, symbols, kind and strategy."""
    q = "SELECT * FROM alerts WHERE 1=1"
    params: list = []
    if start:
        q += " AND date >= ?"
        params.append(start)
    if end:
        q += " AND date <= ?"
        params.append(end)
    if symbols:
        q += f" AND symbol IN ({','.join('?' * len(symbols))})"
        params.extend(s.upper() for s in symbols)
    if kind in ("0dte", "swing", "reversal", "pattern"):
        q += " AND kind = ?"
        params.append(kind)
    if strategy:
        q += " AND strategy = ?"
        params.append(strategy)
    q += " ORDER BY date DESC, alert_time DESC, seq DESC"
    with _db() as con:
        rows = [dict(r) for r in con.execute(q, params)]
    for r in rows:
        r["strategy_label"] = ALL_LABELS.get(r["strategy"], r["strategy"])
    return rows


def performance(strategy: str | None = None, kind: str | None = None) -> dict:
    """How the live alerts have actually done: stats over CLOSED alerts plus
    an equity curve of cumulative P/L by exit date. This is the reality check
    against the backtest — same rules, but measured on logged live signals."""
    q = "SELECT * FROM alerts WHERE 1=1"
    params: list = []
    if strategy:
        q += " AND strategy = ?"
        params.append(strategy)
    if kind in ("0dte", "swing", "reversal", "pattern"):
        q += " AND kind = ?"
        params.append(kind)
    with _db() as con:
        rows = [dict(r) for r in con.execute(q, params)]

    closed = [r for r in rows if r["status"] == "closed" and r["pnl_pct"] is not None]
    out: dict = {
        "signals": sum(1 for r in rows if r["status"] == "signal"),
        "open": sum(1 for r in rows if r["status"] == "open"),
        "closed": len(closed),
        "first_alert": min((r["date"] for r in rows), default=None),
        "last_alert": max((r["date"] for r in rows), default=None),
    }
    if not closed:
        out["curve"] = []
        return out

    pnls = [r["pnl_pct"] for r in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    reasons: dict[str, int] = {}
    for r in closed:
        reasons[r["exit_reason"] or "session_close"] = (
            reasons.get(r["exit_reason"] or "session_close", 0) + 1
        )
    closed.sort(key=lambda r: (r["exit_time"] or "", r["date"]))
    cum = 0.0
    curve = []
    for r in closed:
        cum += r["pnl_pct"]
        curve.append({"date": r["exit_time"] or r["date"], "cum_pnl": round(cum, 2)})
    out.update(
        win_rate=100.0 * len(wins) / len(pnls),
        avg_win=(gross_win / len(wins)) if wins else 0.0,
        avg_loss=(-gross_loss / len(losses)) if losses else 0.0,
        expectancy=sum(pnls) / len(pnls),
        profit_factor=round(gross_win / gross_loss, 2) if gross_loss else None,
        total_pnl=round(sum(pnls), 2),
        exit_reasons=reasons,
        curve=curve,
    )
    return out


# --------------------------------------------------------------------------
# Desktop notifications
# --------------------------------------------------------------------------

def _toast(title: str, body: str) -> None:
    try:
        from winotify import Notification

        Notification(
            app_id="Swing Trade Tracker", title=title, msg=body, duration="long"
        ).show()
    except Exception as exc:  # not on Windows / winotify missing
        print(f"[alert] {title} | {body}  (toast failed: {exc})")


# --------------------------------------------------------------------------
# Live signal evaluation (reuses the zerodte backtest functions)
# --------------------------------------------------------------------------

def _load_today(yf_sym: str) -> pd.DataFrame:
    """Today's 5-minute bars: Alpaca IEX (real-time) with yfinance fallback.

    yf_sym is the Yahoo name ("SPY", "^GSPC"); marketdata handles the mapping
    and falls back to yfinance for cash indices and when Alpaca is down.
    """
    return marketdata.intraday(yf_sym, yf_symbol=yf_sym)


def _prev_close_and_sigma(
    yf_sym: str, as_of: str | None = None
) -> tuple[float | None, float | None]:
    ref = pd.Timestamp(as_of).normalize() if as_of else pd.Timestamp.today().normalize()
    df = zerodte.load_daily(yf_sym, ref - pd.Timedelta(days=45), ref)
    if df.empty or len(df) < 2:
        return None, None
    # exclude the session's own (possibly partial) row
    hist = df[df.index < ref]
    if hist.empty:
        return None, None
    prev_close = float(hist["Close"].iloc[-1])
    sigma = hist["Close"].pct_change().rolling(20).std().iloc[-1]
    return prev_close, (float(sigma) if pd.notna(sigma) else None)


def _trade(bars, direction, entry_i, entry, exit_i, exit_price, stop, exited) -> dict:
    return {
        "direction": "long" if direction == 1 else "short",
        "entry_time": bars.index[entry_i].strftime("%H:%M"),
        "entry_price": entry,
        "exit_time": bars.index[exit_i].strftime("%H:%M"),
        "exit_price": exit_price,
        "pnl_pct": direction * (exit_price - entry) / entry * 100.0,
        "stop_price": stop,
        "exited": exited,
    }


def _orb15_all(bars: pd.DataFrame, session_over: bool) -> list[dict]:
    """Every ORB trade of the session: after a stop-out, the next close back
    outside the opening range (either side) is a fresh entry."""
    trades: list[dict] = []
    if len(bars) < 12:
        return trades
    orh = float(bars["High"].iloc[:3].max())
    orl = float(bars["Low"].iloc[:3].min())
    i = 3
    while i < len(bars):
        direction = entry = entry_i = stop = None
        for k in range(i, len(bars)):
            c = float(bars["Close"].iloc[k])
            if c > orh:
                direction, entry, entry_i, stop = 1, c, k, orl
                break
            if c < orl:
                direction, entry, entry_i, stop = -1, c, k, orh
                break
        if direction is None:
            break
        stopped_at = None
        for j in range(entry_i + 1, len(bars)):
            if (direction == 1 and float(bars["Low"].iloc[j]) <= stop) or (
                direction == -1 and float(bars["High"].iloc[j]) >= stop
            ):
                stopped_at = j
                break
        if stopped_at is not None:
            trades.append(_trade(bars, direction, entry_i, entry, stopped_at, stop, stop, True))
            i = stopped_at + 1  # keep scanning for a re-entry
        else:
            last = len(bars) - 1
            trades.append(
                _trade(bars, direction, entry_i, entry, last,
                       float(bars["Close"].iloc[last]), stop, session_over)
            )
            break
    return trades


def _vwap_all(bars: pd.DataFrame, session_over: bool) -> list[dict]:
    """Every VWAP-reversion trade of the session: after a trade closes at
    VWAP, the next 0.3%+ stretch is a fresh entry."""
    trades: list[dict] = []
    if len(bars) < 12:
        return trades
    tp = (bars["High"] + bars["Low"] + bars["Close"]) / 3.0
    vol = bars["Volume"]
    if float(vol.sum()) > 0:
        vwap = (tp * vol).cumsum() / vol.cumsum()
    else:  # cash indices like ^GSPC have no volume
        vwap = tp.expanding().mean()
    dev = bars["Close"] / vwap - 1.0
    i = 6  # skip the first 30 minutes
    while i < len(bars):
        direction = entry = entry_i = None
        for k in range(i, len(bars)):
            d = float(dev.iloc[k])
            if d >= VWAP_DEV_THRESHOLD:
                direction, entry, entry_i = -1, float(bars["Close"].iloc[k]), k
                break
            if d <= -VWAP_DEV_THRESHOLD:
                direction, entry, entry_i = 1, float(bars["Close"].iloc[k]), k
                break
        if direction is None:
            break
        touched_at = None
        for j in range(entry_i + 1, len(bars)):
            c = float(bars["Close"].iloc[j])
            if (direction == 1 and c >= float(vwap.iloc[j])) or (
                direction == -1 and c <= float(vwap.iloc[j])
            ):
                touched_at = j
                break
        if touched_at is not None:
            trades.append(
                _trade(bars, direction, entry_i, entry, touched_at,
                       float(bars["Close"].iloc[touched_at]), None, True)
            )
            i = touched_at + 1  # keep scanning for a re-entry
        else:
            last = len(bars) - 1
            trades.append(
                _trade(bars, direction, entry_i, entry, last,
                       float(bars["Close"].iloc[last]), None, session_over)
            )
            break
    return trades


def _eval_intraday(name: str, bars: pd.DataFrame, session_over: bool) -> list[dict]:
    """All trades of the session so far for an intraday strategy.

    ORB and VWAP re-enter after an exit (no one-alert-per-day limit);
    Momentum-30 has a single 10:00 decision point, so at most one trade.
    """
    if name == "orb15":
        return _orb15_all(bars, session_over)
    if name == "vwap":
        return _vwap_all(bars, session_over)
    _, func = INTRADAY[name]
    res = func(bars)
    if not res:
        return []
    last_t = bars.index[-1].strftime("%H:%M")
    res["exited"] = session_over or res["exit_time"] != last_t
    return [res]


def _eval_daily(name: str, bars: pd.DataFrame, yf_sym: str, session_over: bool,
                as_of: str | None = None) -> dict | None:
    """Gap fade / straddle sell evaluated live from the session's first bar."""
    if bars.empty:
        return None
    o = float(bars["Open"].iloc[0])
    last = float(bars["Close"].iloc[-1])
    prev_close, sigma = _prev_close_and_sigma(yf_sym, as_of=as_of)
    entry_time = bars.index[0].strftime("%H:%M")
    last_t = bars.index[-1].strftime("%H:%M")
    if name == "gapfade":
        if prev_close is None:
            return None
        gap = o / prev_close - 1.0
        if abs(gap) < GAP_THRESHOLD:
            return None
        direction = -1 if gap > 0 else 1
        return {
            "direction": "short" if direction == -1 else "long",
            "entry_time": entry_time,
            "entry_price": o,
            "exit_time": last_t,
            "exit_price": last,
            "pnl_pct": direction * (last - o) / o * 100.0,
            "stop_price": None,
            "exited": session_over,
        }
    if name == "straddle":
        if sigma is None:
            return None
        move = abs(last / o - 1.0)
        return {
            "direction": "short_vol",
            "entry_time": entry_time,
            "entry_price": o,
            "exit_time": last_t,
            "exit_price": last,
            "pnl_pct": float((STRADDLE_PREMIUM_MULT * sigma - move) * 100.0),
            "stop_price": None,
            "exited": session_over,
        }
    return None


# --------------------------------------------------------------------------
# Reconciliation: close 0DTE rows orphaned in 'open' state
# --------------------------------------------------------------------------

def _direction_mult(direction: str) -> int:
    return -1 if direction in ("short", "short_vol") else 1


def _load_session(yf_sym: str, date: str) -> pd.DataFrame:
    """5-minute bars for one session (empty beyond yfinance's ~60-day window)."""
    try:
        df = yf.Ticker(yf_sym).history(
            start=date,
            end=str((pd.Timestamp(date) + pd.Timedelta(days=1)).date()),
            interval="5m",
            auto_adjust=True,
        )
    except Exception as exc:
        print(f"[alert] session fetch failed for {yf_sym} {date}: {exc}")
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    df = _flatten(df)
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_convert("America/New_York").tz_localize(None)
    return df.dropna(subset=["Close"])


def _fallback_close(row: dict, yf_sym: str, date: str, bars: pd.DataFrame) -> dict | None:
    """Close a stale row at the session close when an exact replay isn't possible."""
    if not bars.empty:
        last = float(bars["Close"].iloc[-1])
    else:
        day = zerodte.load_daily(yf_sym, pd.Timestamp(date), pd.Timestamp(date))
        day = day[day.index == pd.Timestamp(date)]
        if day.empty:
            return None
        last = float(day["Close"].iloc[-1])
    entry = row["entry_price"]
    if entry is None:
        return None
    if row["strategy"] == "straddle":
        _, sigma = _prev_close_and_sigma(yf_sym, as_of=date)
        if sigma is None:
            return None
        pnl = float((STRADDLE_PREMIUM_MULT * sigma - abs(last / entry - 1.0)) * 100.0)
    else:
        pnl = _direction_mult(row["direction"]) * (last - entry) / entry * 100.0
    return {"exit_time": "16:00", "exit_price": last, "pnl_pct": pnl}


def close_stale_0dte(now: datetime) -> int:
    """Close 0DTE rows left 'open' after their session ended.

    Exits are normally written by the poll loop during the 16:00-16:10 grace
    window. If the monitor was stopped at the close, started late, or the
    machine slept, rows stay 'open' forever. This replays each stale session
    with the same evaluators and records the missed exits (prints instead of
    toasts -- these are old news). Returns the number of rows closed.
    """
    today = now.strftime("%Y-%m-%d")
    q = "SELECT * FROM alerts WHERE kind='0dte' AND status='open' AND date < ?"
    params = [today]
    if now.strftime("%H:%M") >= "16:10":  # today's session is over too
        q = q.replace("date < ?", "(date < ? OR date = ?)")
        params.append(today)
    with _db() as con:
        rows = [dict(r) for r in con.execute(q, params)]
    if not rows:
        return 0
    closed = 0
    by_session: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        by_session.setdefault((r["date"], r["symbol"]), []).append(r)
    for (date, sym), srows in by_session.items():
        yf_sym = SYMBOLS.get(sym)
        if yf_sym is None:
            continue
        bars = _load_session(yf_sym, date)
        for row in srows:
            res = None
            name = row["strategy"]
            if not bars.empty:
                if name in INTRADAY:
                    trades = _eval_intraday(name, bars, session_over=True)
                    if row["seq"] < len(trades):
                        res = trades[row["seq"]]
                else:
                    res = _eval_daily(name, bars, yf_sym, True, as_of=date)
            if res is None:
                res = _fallback_close(row, yf_sym, date, bars)
            if res is None:
                continue  # no data at all; try again next reconcile
            with _db() as con:
                con.execute(
                    """UPDATE alerts SET exit_time=?, exit_price=?, pnl_pct=?,
                        exit_reason='reconciled', status='closed' WHERE id=?""",
                    (res["exit_time"], res["exit_price"], res["pnl_pct"], row["id"]),
                )
            closed += 1
            print(
                f"[alert] reconciled {sym} {name} #{row['seq']} {date}: "
                f"exit {res['exit_time']} @ {res['exit_price']:.2f} ({res['pnl_pct']:+.2f}%)"
            )
    return closed


# --------------------------------------------------------------------------
# Swing alerts: signal on the close -> fill at next open -> daily exit checks
# (mirrors the backtest execution model in backtest.py)
# --------------------------------------------------------------------------

def _market_hours(now: datetime) -> bool:
    return now.weekday() < 5 and "09:30" <= now.strftime("%H:%M") <= "16:00"


def _record_swing_signal(sig: dict, key: str, strat, now: datetime) -> None:
    """Insert a new swing buy-signal alert; toast only when it is new."""
    atr = sig.get("atr") or (sig["close"] - sig["stop"]) / strat.stop_atr_mult
    with _db() as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO alerts (kind, alert_time, date, symbol, strategy,
                seq, direction, entry_time, stop_price, target_price, atr_entry,
                planned_exit, status)
                VALUES ('swing',?,?,?,?,0,'long','next open',?,?,?,?,'signal')""",
            (
                now.strftime("%H:%M:%S"), sig["date"], sig["ticker"], key,
                sig["stop"], sig.get("target"), atr, _swing_planned_exit(strat),
            ),
        )
        is_new = bool(cur.rowcount)
        row = con.execute(
            """SELECT id, status, broker_order_id FROM alerts WHERE kind='swing'
                AND date=? AND symbol=? AND strategy=? AND seq=0""",
            (sig["date"], sig["ticker"], key),
        ).fetchone()
    if is_new:
        tgt = f" | Target ≈ {sig['target']:.2f}" if sig.get("target") else ""
        _toast(
            f"SWING SIGNAL: {sig['ticker']} — {strat.label}",
            f"Close {sig['close']:.2f} on {sig['date']} | Buy at next open"
            f" | Stop ≈ {sig['stop']:.2f}{tgt} | Exit: {_swing_planned_exit(strat)}",
        )
    # Paper order — only on a COMPLETED daily bar (market closed). Signals
    # seen intraday are provisional (partial bar) and a market order would
    # also fill immediately instead of at the next open; the monitor rescans
    # after the close and mirrors the signals that survived. GTC market
    # orders submitted after hours queue and fill at the next open.
    if (
        row is not None
        and row["status"] == "signal"
        and not row["broker_order_id"]
        and not _market_hours(now)
    ):
        order = broker.buy_bracket(
            sig["ticker"], sig["close"], sig["stop"], sig.get("target")
        )
        if order:
            with _db() as con:
                con.execute(
                    "UPDATE alerts SET broker_order_id=?, broker_qty=? WHERE id=?",
                    (order["id"], order["qty"], row["id"]),
                )
            _toast(
                f"PAPER ORDER: {sig['ticker']} {order['qty']} shares — {strat.label}",
                f"~${order['notional']:,.0f} bracket buy sent to Alpaca paper"
                f" (fills at next open) | Stop {sig['stop']:.2f}",
            )


def _track_swing_positions(earnings_buffer: int, now: datetime) -> None:
    """Fill pending swing signals at the next session's open and run the
    strategy exit rules (stop, target, signal, time stop, pre-earnings) on
    every bar since entry — reconstructing correctly even if the monitor
    was offline for days."""
    with _db() as con:
        rows = [
            dict(r) for r in con.execute(
                "SELECT * FROM alerts WHERE kind='swing' AND status IN ('signal','open')"
            )
        ]
    by_ticker: dict[str, list[dict]] = {}
    for r in rows:
        by_ticker.setdefault(r["symbol"], []).append(r)

    for ticker, trows in by_ticker.items():
        df = load_history(ticker, days=460)  # warmup for exit indicators
        if df.empty:
            continue
        edates = earnings_dates(ticker)
        for row in trows:
            strat = SWING_STRATEGIES.get(row["strategy"])
            if strat is None:
                continue
            after = df.index[df.index > pd.Timestamp(row["date"])]
            if len(after) == 0:
                continue  # next session hasn't opened yet

            if row["status"] == "signal":  # fill at the next session's open
                entry_ts = after[0]
                entry = float(df.loc[entry_ts, "Open"])
                if strat.absolute_levels:  # chart levels fixed at signal time
                    stop = row["stop_price"]
                    if stop is None or stop >= entry:
                        stop = entry - strat.stop_atr_mult * row["atr_entry"]
                    target = row["target_price"]
                    if target is not None and target <= entry * 1.01:
                        target = None
                else:
                    stop = entry - strat.stop_atr_mult * row["atr_entry"]
                    target = (
                        entry + strat.target_atr_mult * row["atr_entry"]
                        if strat.target_atr_mult else None
                    )
                with _db() as con:
                    con.execute(
                        """UPDATE alerts SET entry_time=?, entry_price=?, stop_price=?,
                            target_price=?, status='open' WHERE id=?""",
                        (str(entry_ts.date()), entry, stop, target, row["id"]),
                    )
                row.update(entry_time=str(entry_ts.date()), entry_price=entry,
                           stop_price=stop, target_price=target, status="open")
                tgt_txt = f" | Target {target:.2f}" if target else ""
                _toast(
                    f"SWING ENTRY: {ticker} @ {entry:.2f} — {strat.label}",
                    f"Filled at the {str(entry_ts.date())} open"
                    f" | Stop {stop:.2f}{tgt_txt}",
                )

            if row["status"] != "open":
                continue
            entry_idx = df.index.get_indexer([pd.Timestamp(row["entry_time"])])[0]
            if entry_idx < 0:
                continue
            stop, target = row["stop_price"], row["target_price"]
            for j in range(entry_idx, len(df)):
                bar = df.iloc[j]
                reason = strat.exit(bar, stop, target, j - entry_idx)
                if reason is None and j + 1 < len(df) and near_earnings(
                    df.index[j + 1], edates, earnings_buffer
                ):
                    reason = "pre_earnings"
                if reason is None:
                    continue
                if reason == "stop":
                    exit_price = stop
                elif reason == "profit_target":
                    exit_price = target
                else:
                    exit_price = float(bar["Close"])
                pnl = (exit_price / row["entry_price"] - 1.0) * 100.0
                with _db() as con:
                    con.execute(
                        """UPDATE alerts SET exit_time=?, exit_price=?, pnl_pct=?,
                            exit_reason=?, status='closed' WHERE id=?""",
                        (str(df.index[j].date()), exit_price, pnl, reason, row["id"]),
                    )
                _toast(
                    f"SWING EXIT: {ticker} {'+' if pnl >= 0 else ''}{pnl:.2f}% ({reason})",
                    f"Exit {str(df.index[j].date())} @ {exit_price:.2f}"
                    f" (entered {row['entry_time']} @ {row['entry_price']:.2f})"
                    f" — {strat.label}",
                )
                # Flatten the mirrored paper position. Safe for stop/target
                # exits too: the bracket leg usually filled broker-side
                # already, in which case this is a quiet no-op.
                if row.get("broker_order_id"):
                    broker.close(ticker)
                break


# --------------------------------------------------------------------------
# Monitor
# --------------------------------------------------------------------------

class AlertMonitor:
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.symbols: list[str] = []
        self.strategies: list[str] = []
        self.swing_universe: str | None = None
        self.swing_tickers: list[str] = []
        self.swing_strategies: list[str] = []
        self.reversal_tickers: list[str] = []
        self._last_rev_ts: float | None = None
        self.pattern_on: bool = False
        self._last_pat_ts: float | None = None
        self.last_pattern_poll: str | None = None
        self.earnings_buffer: int = 3
        self.last_poll: str | None = None
        self.last_swing_poll: str | None = None
        self._last_swing_ts: float | None = None
        self._last_eod_date: str | None = None
        self._flattened_date: str | None = None
        self._reconciled_key: str | None = None
        self.started_at: str | None = None

    # -- public API --------------------------------------------------------

    def start(
        self,
        symbols: list[str] | None = None,
        strategies: list[str] | None = None,
        swing_universe: str | None = None,
        swing_strategies: list[str] | None = None,
        swing_tickers: list[str] | None = None,
        earnings_buffer: int | None = None,
        reversal_tickers: list[str] | None = None,
        pattern_on: bool | None = None,
        configure: tuple[str, ...] = ("zerodte", "swing"),
    ) -> dict:
        """Start (or reconfigure) the monitor.

        ``configure`` says which channels this call owns. The 0DTE tab sends
        ("zerodte",), the swing/bear tabs send ("swing",) and the analyzer tab
        sends ("reversal",), so starting one never wipes the others -- all
        three channels run in the same background loop.
        """
        if "reversal" in configure:
            seen: set = set()
            self.reversal_tickers = [
                t.upper() for t in (reversal_tickers or [])
                if t.strip() and not (t.upper() in seen or seen.add(t.upper()))
            ]
            self._last_rev_ts = None  # scan immediately with the new list
        if "pattern" in configure:
            self.pattern_on = bool(pattern_on)
            self._last_pat_ts = None  # evaluate immediately with the armed set
        if "zerodte" in configure:
            # An empty symbols list disables 0DTE alerts.
            self.symbols = [s.upper() for s in (symbols or []) if s.upper() in SYMBOLS]
            self.strategies = [
                s for s in (strategies or []) if s in STRATEGY_LABELS
            ] or list(STRATEGY_LABELS)

        if "swing" in configure:
            self.swing_universe = (
                swing_universe if swing_universe and swing_universe != "none" else None
            )
            self.swing_strategies = [
                s for s in (swing_strategies or []) if s in SWING_STRATEGIES
            ]
            self.swing_tickers = []
            if self.swing_universe and self.swing_strategies:
                base: list[str] = []
                if self.swing_universe != "custom":
                    try:
                        base = get_universe(self.swing_universe)
                    except Exception as exc:
                        print(f"[alert] failed to load universe {self.swing_universe}: {exc}")
                extra = [t.upper() for t in (swing_tickers or []) if t.strip()]
                seen = set()
                self.swing_tickers = [
                    t for t in base + extra if not (t in seen or seen.add(t))
                ]
            if earnings_buffer is not None:
                self.earnings_buffer = int(earnings_buffer)
            self._last_swing_ts = None  # rescan immediately with the new config

        if self.running:  # already polling: the new config takes effect next tick
            return self.status()
        self._last_swing_ts = None
        # A start after 16:15 makes the immediate first tick the EOD scan.
        now = datetime.now(NY)
        self._last_eod_date = (
            now.strftime("%Y-%m-%d") if now.strftime("%H:%M") >= "16:15" else None
        )
        self._reconciled_key = None
        self._stop.clear()
        self.started_at = datetime.now(NY).isoformat(timespec="seconds")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self.status()

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        return self.status()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        now = datetime.now(NY)
        return {
            "running": self.running,
            "symbols": self.symbols,
            "strategies": self.strategies,
            "swing_universe": self.swing_universe,
            "swing_tickers": len(self.swing_tickers),
            "swing_strategies": self.swing_strategies,
            "reversal_tickers": self.reversal_tickers,
            "pattern_on": self.pattern_on,
            "last_pattern_poll": self.last_pattern_poll,
            "started_at": self.started_at,
            "last_poll": self.last_poll,
            "last_swing_poll": self.last_swing_poll,
            "market_open": self._market_open(now),
            "now_et": now.strftime("%Y-%m-%d %H:%M:%S ET"),
            "paper_0dte": broker.zerodte_strategies() if broker.enabled() else [],
        }

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _market_open(now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        hm = now.strftime("%H:%M")
        return "09:30" <= hm <= "16:10"  # small grace window to settle exits

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = datetime.now(NY)
            # Close orphaned 0DTE rows: once on start (catches sessions the
            # monitor missed entirely) and once after each 16:10 cutoff (in
            # case the close-window polls failed). No-op when nothing is stale.
            key = now.strftime("%Y-%m-%d") if now.strftime("%H:%M") >= "16:10" else "startup"
            if self._reconciled_key != key:
                try:
                    close_stale_0dte(now)
                except Exception as exc:
                    print(f"[alert] reconcile error: {exc}")
                self._reconciled_key = key
            if self.symbols and self._market_open(now):
                try:
                    self._poll(now)
                except Exception as exc:
                    print(f"[alert] poll error: {exc}")
                self.last_poll = now.isoformat(timespec="seconds")
            # 0DTE safety net: flatten any paper position in the monitored
            # symbols just before the close. Nothing intraday carries
            # overnight, even if an exit alert was missed. Swing positions
            # are untouched (the sweep is scoped to the 0DTE symbols).
            if (
                self.symbols
                and now.weekday() < 5
                and "15:55" <= now.strftime("%H:%M") <= "16:05"
                and self._flattened_date != now.strftime("%Y-%m-%d")
            ):
                try:
                    broker.flatten(self.symbols)
                except Exception as exc:
                    print(f"[alert] flatten error: {exc}")
                self._flattened_date = now.strftime("%Y-%m-%d")
            # Swing tick: once immediately on start (catches the latest close's
            # signals even outside market hours), then every 15 min while open.
            if self.swing_tickers and self.swing_strategies and (
                self._last_swing_ts is None
                or (
                    self._market_open(now)
                    and time.time() - self._last_swing_ts >= SWING_POLL_SECONDS
                )
            ):
                try:
                    self._swing_tick(now)
                except Exception as exc:
                    print(f"[alert] swing tick error: {exc}")
                self._last_swing_ts = time.time()
                self.last_swing_poll = now.isoformat(timespec="seconds")
            # Reversal watch (analyzer tab): once on start, then every 15 min
            # while the market is open. Alert-only -- never trades.
            if self.reversal_tickers and (
                self._last_rev_ts is None
                or (
                    self._market_open(now)
                    and time.time() - self._last_rev_ts >= SWING_POLL_SECONDS
                )
            ):
                try:
                    self._reversal_tick(now)
                except Exception as exc:
                    print(f"[alert] reversal tick error: {exc}")
                self._last_rev_ts = time.time()
            # Pattern watch (Pattern lab tab): once on start, then every 5 min
            # while open. Fires when today's live conditions match an armed
            # >win% pattern; tracks each to its +$/-$ barrier or the close.
            if self.pattern_on and (
                self._last_pat_ts is None
                or (
                    self._market_open(now)
                    and time.time() - self._last_pat_ts >= PATTERN_POLL_SECONDS
                )
            ):
                try:
                    self._pattern_tick(now)
                except Exception as exc:
                    print(f"[alert] pattern tick error: {exc}")
                self._last_pat_ts = time.time()
                self.last_pattern_poll = now.isoformat(timespec="seconds")
            # End-of-day confirmation scan: intraday signals are provisional
            # (partial daily bar). One rescan on the completed bar confirms
            # which survived to the close and submits their paper orders.
            if (
                self.swing_tickers and self.swing_strategies
                and now.weekday() < 5
                and now.strftime("%H:%M") >= "16:15"
                and self._last_eod_date != now.strftime("%Y-%m-%d")
            ):
                try:
                    self._swing_tick(now)
                except Exception as exc:
                    print(f"[alert] EOD swing tick error: {exc}")
                self._last_eod_date = now.strftime("%Y-%m-%d")
                self.last_swing_poll = now.isoformat(timespec="seconds")
            self._stop.wait(POLL_SECONDS)

    def _reversal_tick(self, now: datetime) -> None:
        """Toast + log fresh reversal evidence on the watched tickers.

        Deduped per (date, symbol, direction) by the alerts UNIQUE key, so a
        reversal that stays in place all day alerts once, not every 15 min.
        """
        today = now.strftime("%Y-%m-%d")
        for ev in analyzer.scan_reversals(self.reversal_tickers):
            key = "rev_bull" if ev["direction"] == "bullish" else "rev_bear"
            with _db() as con:
                cur = con.execute(
                    """INSERT OR IGNORE INTO alerts (kind, alert_time, date, symbol,
                        strategy, seq, direction, entry_time, entry_price,
                        planned_exit, status)
                        VALUES ('reversal',?,?,?,?,0,?,?,?,?,'signal')""",
                    (
                        now.strftime("%H:%M:%S"), today, ev["ticker"], key,
                        "long" if ev["direction"] == "bullish" else "short",
                        now.strftime("%H:%M"), ev["price"], ev["what"],
                    ),
                )
            if cur.rowcount:
                _toast(
                    f"REVERSAL ({ev['direction'].upper()}): {ev['ticker']} @ {ev['price']:.2f}",
                    ev["what"] + " — informational alert, no order placed",
                )

    def _pattern_tick(self, now: datetime) -> None:
        """For each armed ticker, alert when today's live conditions match one
        of THAT ticker's own >win% patterns, then walk each open pattern trade
        to its +$/-$ barrier or the close.

        Each ticker is matched only against the patterns from its own study
        (arm() ties them together), so QQQ alerts fire off the QQQ deep dive
        and AAPL off the AAPL one. Entries are deduped per (date, symbol,
        pattern) by the alerts UNIQUE key. Alert-only: no paper order.
        """
        store = patterns.armed()
        if not store:
            return
        today = now.strftime("%Y-%m-%d")
        for tkr, cfg in store["tickers"].items():
            target = float(cfg["target"])
            snap = patterns.live_features(tkr)
            if snap.get("ready"):
                P = float(snap["price"])
                for p in patterns.match_live(snap["features"], cfg["patterns"]):
                    long = p["side"] == "long"
                    tgt, stop = (P + target, P - target) if long else (P - target, P + target)
                    planned = f"+${target:g} move ({p['win_pct']}% hist, ~{p['avg_mins'] or '?'} min)"
                    with _db() as con:
                        cur = con.execute(
                            """INSERT OR IGNORE INTO alerts (kind, alert_time, date, symbol,
                                strategy, seq, direction, entry_time, entry_price,
                                stop_price, target_price, planned_exit, status)
                                VALUES ('pattern',?,?,?,?,0,?,?,?,?,?,?,'open')""",
                            (now.strftime("%H:%M:%S"), today, tkr, p["label"],
                             "long" if long else "short", snap["tod"], P, stop, tgt, planned),
                        )
                    if cur.rowcount:
                        _toast(
                            f"PATTERN {p['side'].upper()} {p['win_pct']}%: {tkr} @ {P:.2f}",
                            f"{p['label']} → target {tgt:.2f} / stop {stop:.2f} (informational)",
                        )
            self._settle_pattern_trades(tkr, now)

    def _settle_pattern_trades(self, tkr: str, now: datetime) -> None:
        today = now.strftime("%Y-%m-%d")
        with _db() as con:
            opens = [dict(r) for r in con.execute(
                "SELECT * FROM alerts WHERE kind='pattern' AND status='open' "
                "AND date=? AND symbol=?", (today, tkr),
            )]
        if not opens:
            return
        bars = marketdata.intraday(tkr, yf_symbol=tkr)
        if bars.empty:
            return
        session_over = now.strftime("%H:%M") >= SESSION_END
        for r in opens:
            after = bars[bars.index.strftime("%H:%M") >= (r["entry_time"] or "09:30")]
            if after.empty:
                continue
            long = r["direction"] == "long"
            hi, lo = float(after["High"].max()), float(after["Low"].min())
            hit_tgt = hi >= r["target_price"] if long else lo <= r["target_price"]
            hit_stop = lo <= r["stop_price"] if long else hi >= r["stop_price"]
            if hit_tgt and not hit_stop:
                exit_price, reason = r["target_price"], "target"
            elif hit_stop and not hit_tgt:
                exit_price, reason = r["stop_price"], "stop"
            elif hit_tgt and hit_stop:
                # both barriers inside the polling window -- conservative: stop
                exit_price, reason = r["stop_price"], "stop_ambig"
            elif session_over:
                exit_price, reason = float(after["Close"].iloc[-1]), "session_close"
            else:
                continue
            entry = r["entry_price"]
            pnl = (exit_price - entry) / entry * 100 if long else (entry - exit_price) / entry * 100
            with _db() as con:
                con.execute(
                    "UPDATE alerts SET exit_time=?, exit_price=?, pnl_pct=?, "
                    "exit_reason=?, status='closed' WHERE id=?",
                    (now.strftime("%H:%M"), round(exit_price, 2), round(pnl, 3), reason, r["id"]),
                )
            _toast(
                f"PATTERN EXIT ({reason}): {tkr} @ {exit_price:.2f}",
                f"{r['strategy']} → {'+' if pnl >= 0 else ''}{pnl:.2f}%",
            )

    def _swing_tick(self, now: datetime) -> None:
        strats = [SWING_STRATEGIES[k] for k in self.swing_strategies]
        label_to_key = {s.label: s.name for s in strats}
        for sig in screener.scan(
            self.swing_tickers, strats, earnings_buffer=self.earnings_buffer
        ):
            key = label_to_key.get(sig["strategy"])
            if key:
                _record_swing_signal(sig, key, SWING_STRATEGIES[key], now)
        _track_swing_positions(self.earnings_buffer, now)

    def _poll(self, now: datetime) -> None:
        session_over = now.strftime("%H:%M") >= SESSION_END
        today = now.strftime("%Y-%m-%d")
        for sym in self.symbols:
            bars = _load_today(SYMBOLS[sym])
            if bars.empty:
                continue
            # keep only today's session (guards against yfinance returning
            # the prior session before the open)
            bars = bars[bars.index.strftime("%Y-%m-%d") == today]
            if bars.empty:
                continue
            for name in self.strategies:
                if name in INTRADAY:
                    trades = _eval_intraday(name, bars, session_over)
                else:
                    res = _eval_daily(name, bars, SYMBOLS[sym], session_over)
                    trades = [res] if res else []
                for seq, res in enumerate(trades):
                    self._record(today, sym, name, seq, res, now)

    def _record(self, date: str, sym: str, name: str, seq: int, res: dict, now: datetime) -> None:
        label = STRATEGY_LABELS[name]
        nth = f" #{seq + 1}" if seq else ""
        with _db() as con:
            row = con.execute(
                """SELECT id, status, broker_order_id FROM alerts
                    WHERE date=? AND symbol=? AND strategy=? AND seq=?""",
                (date, sym, name, seq),
            ).fetchone()

            if row is None:  # new entry alert
                con.execute(
                    """INSERT INTO alerts (alert_time, date, symbol, strategy, seq, direction,
                        entry_time, entry_price, stop_price, planned_exit, status)
                        VALUES (?,?,?,?,?,?,?,?,?,?,'open')""",
                    (
                        now.strftime("%H:%M:%S"), date, sym, name, seq, res["direction"],
                        res["entry_time"], res["entry_price"], res.get("stop_price"),
                        PLANNED_EXITS.get(name, SESSION_END),
                    ),
                )
                stop = res.get("stop_price")
                stop_txt = f" | Stop {stop:.2f}" if stop else ""
                _toast(
                    f"0DTE ENTRY{nth}: {sym} {label} ({res['direction'].upper()})",
                    f"Entry {res['entry_time']} @ {res['entry_price']:.2f}{stop_txt}"
                    f" | Exit: {PLANNED_EXITS.get(name, SESSION_END)}",
                )
                row = con.execute(
                    """SELECT id, status, broker_order_id FROM alerts
                        WHERE date=? AND symbol=? AND strategy=? AND seq=?""",
                    (date, sym, name, seq),
                ).fetchone()

                # Paper trade: only the strategies in broker.zerodte_strategies
                # (VWAP by default). Every other strategy still alerts and logs
                # above -- it just never reaches the broker. Entries are placed
                # live during the session, never after the exit already printed.
                if not res.get("exited") and self._market_open(now) and \
                        now.strftime("%H:%M") < SESSION_END:
                    order = broker.zerodte_order(
                        sym, name, res["direction"], res["entry_price"]
                    )
                    if order:
                        con.execute(
                            "UPDATE alerts SET broker_order_id=?, broker_qty=? WHERE id=?",
                            (order["id"], order["qty"], row["id"]),
                        )
                        row = con.execute(
                            "SELECT id, status, broker_order_id FROM alerts WHERE id=?",
                            (row["id"],),
                        ).fetchone()
                        _toast(
                            f"PAPER 0DTE{nth}: {sym} {order['side'].upper()} "
                            f"{order['qty']} shares — {label}",
                            f"~${order['notional']:,.0f} market order sent to Alpaca paper"
                            f" | Exit: {PLANNED_EXITS.get(name, SESSION_END)}",
                        )

            if row["status"] == "open" and res.get("exited"):
                con.execute(
                    """UPDATE alerts SET exit_time=?, exit_price=?, pnl_pct=?,
                        status='closed' WHERE id=?""",
                    (res["exit_time"], res["exit_price"], res["pnl_pct"], row["id"]),
                )
                pnl = res["pnl_pct"]
                _toast(
                    f"0DTE EXIT{nth}: {sym} {label} {'+' if pnl >= 0 else ''}{pnl:.2f}%",
                    f"Exit {res['exit_time']} @ {res['exit_price']:.2f}"
                    f" (entered {res['entry_time']} @ {res['entry_price']:.2f})",
                )
                if row["broker_order_id"]:
                    broker.close(sym)   # VWAP touched, or the session ended


MONITOR = AlertMonitor()
