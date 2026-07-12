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

from . import screener, zerodte
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
SESSION_END = "16:00"

PLANNED_EXITS = {
    "orb15": "stop or 16:00 close",
    "vwap": "VWAP touch or 16:00 close",
    "momentum30": "16:00 close",
    "gapfade": "16:00 close",
    "straddle": "16:00 close (theta decay)",
}

# strategy key -> label across both alert kinds
ALL_LABELS = {**STRATEGY_LABELS, **{k: s.label for k, s in SWING_STRATEGIES.items()}}


def _swing_planned_exit(s) -> str:
    return s.planned_exit()


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

_COLUMNS = [
    "kind", "alert_time", "date", "symbol", "strategy", "seq", "direction",
    "entry_time", "entry_price", "stop_price", "target_price", "atr_entry",
    "planned_exit", "exit_time", "exit_price", "pnl_pct", "exit_reason", "status",
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
    UNIQUE(date, symbol, strategy, seq)
)"""


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute(_SCHEMA)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(alerts)")}
    if not set(_COLUMNS) <= cols:  # older schema: rebuild, keeping shared columns
        keep = ", ".join(c for c in _COLUMNS if c in cols)
        con.execute("ALTER TABLE alerts RENAME TO alerts_old")
        con.execute(_SCHEMA)
        con.execute(f"INSERT INTO alerts ({keep}) SELECT {keep} FROM alerts_old")
        con.execute("DROP TABLE alerts_old")
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
    if kind in ("0dte", "swing"):
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
    if kind in ("0dte", "swing"):
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
    try:
        df = yf.Ticker(yf_sym).history(period="1d", interval="5m", auto_adjust=True)
    except Exception as exc:
        print(f"[alert] intraday fetch failed for {yf_sym}: {exc}")
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    df = _flatten(df)
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_convert("America/New_York").tz_localize(None)
    return df.dropna(subset=["Close"])


def _prev_close_and_sigma(yf_sym: str) -> tuple[float | None, float | None]:
    df = zerodte.load_daily(
        yf_sym, pd.Timestamp.today().normalize() - pd.Timedelta(days=45),
        pd.Timestamp.today().normalize(),
    )
    if df.empty or len(df) < 2:
        return None, None
    # exclude today's partial row if present
    hist = df[df.index < pd.Timestamp.today().normalize()]
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


def _eval_daily(name: str, bars: pd.DataFrame, yf_sym: str, session_over: bool) -> dict | None:
    """Gap fade / straddle sell evaluated live from today's first bar."""
    if bars.empty:
        return None
    o = float(bars["Open"].iloc[0])
    last = float(bars["Close"].iloc[-1])
    prev_close, sigma = _prev_close_and_sigma(yf_sym)
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
# Swing alerts: signal on the close -> fill at next open -> daily exit checks
# (mirrors the backtest execution model in backtest.py)
# --------------------------------------------------------------------------

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
        if cur.rowcount:
            tgt = f" | Target ≈ {sig['target']:.2f}" if sig.get("target") else ""
            _toast(
                f"SWING SIGNAL: {sig['ticker']} — {strat.label}",
                f"Close {sig['close']:.2f} on {sig['date']} | Buy at next open"
                f" | Stop ≈ {sig['stop']:.2f}{tgt} | Exit: {_swing_planned_exit(strat)}",
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
        self.earnings_buffer: int = 3
        self.last_poll: str | None = None
        self.last_swing_poll: str | None = None
        self._last_swing_ts: float | None = None
        self.started_at: str | None = None

    # -- public API --------------------------------------------------------

    def start(
        self,
        symbols: list[str],
        strategies: list[str],
        swing_universe: str | None = None,
        swing_strategies: list[str] | None = None,
        swing_tickers: list[str] | None = None,
        earnings_buffer: int = 3,
    ) -> dict:
        if self.running:
            return self.status()
        # An empty symbols list disables 0DTE alerts (swing-only monitor).
        self.symbols = [s.upper() for s in symbols if s.upper() in SYMBOLS]
        self.strategies = [s for s in strategies if s in STRATEGY_LABELS] or list(STRATEGY_LABELS)
        self.swing_universe = swing_universe if swing_universe and swing_universe != "none" else None
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
        self.earnings_buffer = int(earnings_buffer)
        self._last_swing_ts = None
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
            "started_at": self.started_at,
            "last_poll": self.last_poll,
            "last_swing_poll": self.last_swing_poll,
            "market_open": self._market_open(now),
            "now_et": now.strftime("%Y-%m-%d %H:%M:%S ET"),
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
            if self.symbols and self._market_open(now):
                try:
                    self._poll(now)
                except Exception as exc:
                    print(f"[alert] poll error: {exc}")
                self.last_poll = now.isoformat(timespec="seconds")
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
            self._stop.wait(POLL_SECONDS)

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
                "SELECT id, status FROM alerts WHERE date=? AND symbol=? AND strategy=? AND seq=?",
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
                    "SELECT id, status FROM alerts WHERE date=? AND symbol=? AND strategy=? AND seq=?",
                    (date, sym, name, seq),
                ).fetchone()

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


MONITOR = AlertMonitor()
