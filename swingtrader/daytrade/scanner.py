"""Replay the intraday relative-strength scanner bar by bar.

Definitions (mirroring the Market Bishop scan):

- iRS: how far the price sits above/below the middle of today's high-low
  range so far, in % of that midpoint, ranked 0-99 across the universe at
  each 5-minute step (99 = strongest).
- 5D: move vs the close 5 sessions ago, ranked the same way, live.
- 20D: prior close vs the close 20 sessions before it, ranked once per day.
- A signal is the FIRST time a ticker enters a side's band on a day:
  calls iRS >= 95, puts iRS <= 5 by default (the screenshots' bands). The
  wide setting (90 / 10) also records the near bands; a stock climbing
  through 90-94 then fires there first. Time is the bar's close
  (09:30 bar -> 09:35) and the entry is that bar's close.

Outcomes use only the bars AFTER the signal bar, through the session close:
favorable/adverse moves are % in the trade's direction (puts profit when
the stock falls).
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .data import regular_session
from .universe import theme_of

CALL_BAND = 95
PUT_BAND = 5
WIDE_CALL_BAND = 90
WIDE_PUT_BAND = 10
MIN_PRICE = 5.0
HIT_PCT = 2.0
CLUSTER_WINDOW_MIN = 60


@dataclass
class Signal:
    date: str
    time: str
    ticker: str
    theme: str
    side: str            # "call" or "put"
    irs: int
    r5: int
    r20: int
    day_pct: float       # % vs prior close at the signal
    gap_pct: float       # today's open vs prior close, %
    entry: float
    mfe: float           # max favorable move after entry, % (>= 0)
    mae: float           # max adverse move after entry, % (<= 0)
    close_ret: float     # % at the session close, in trade direction
    mins_to_hit: float | None  # minutes until +HIT_PCT, None if never
    # running context, known at signal time (no look-ahead)
    calls_so_far: int = 0
    puts_so_far: int = 0
    cluster_n: int = 1
    # per-bar forward path for exit simulation (not serialized)
    fwd_fav: np.ndarray = field(default=None, repr=False)
    fwd_adv: np.ndarray = field(default=None, repr=False)
    fwd_close: np.ndarray = field(default=None, repr=False)

    @property
    def band(self) -> str:
        if self.side == "call":
            return "CALLS · iRS 95-99" if self.irs >= 95 else "CALLS · iRS 90-94"
        return "PUTS · iRS 0-5" if self.irs <= 5 else "PUTS · iRS 6-10"

    @property
    def minutes(self) -> int:
        h, m = map(int, self.time.split(":"))
        return h * 60 + m

    def hit(self, pct: float = HIT_PCT) -> bool:
        return self.mfe >= pct

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if not k.startswith("fwd_")}
        d["band"] = self.band
        return d


def rank99(frame: pd.DataFrame) -> pd.DataFrame:
    """Row-wise percentile rank scaled to 0-99 (NaN stays NaN)."""
    r = frame.rank(axis=1, method="average")
    n = frame.notna().sum(axis=1)
    scaled = (r.sub(1)).div((n - 1).where(n > 1), axis=0) * 99
    return scaled.round()


def _wide(intraday: dict[str, pd.DataFrame], col: str) -> pd.DataFrame:
    return pd.DataFrame({t: df[col] for t, df in intraday.items()})


def _daily_refs(daily: dict[str, pd.DataFrame], tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Per-date reference closes: prior close, close 5 sessions back, and
    the 20-session return as of the prior close."""
    closes = pd.DataFrame({t: daily[t]["Close"] for t in tickers if t in daily})
    closes.index = pd.to_datetime(closes.index).normalize()
    closes = closes.sort_index()
    return {
        "prev": closes.shift(1),
        "back5": closes.shift(5),
        "ret20": closes.shift(1) / closes.shift(21) - 1,
    }


def _forward(side: str, entry: float, hi: np.ndarray, lo: np.ndarray, cl: np.ndarray):
    if side == "call":
        fav, adv, close = hi / entry - 1, lo / entry - 1, cl / entry - 1
    else:
        fav, adv, close = 1 - lo / entry, 1 - hi / entry, 1 - cl / entry
    return fav * 100, adv * 100, close * 100


def scan_day(
    date,
    o: pd.DataFrame,
    h: pd.DataFrame,
    lo: pd.DataFrame,
    c: pd.DataFrame,
    prev: pd.Series,
    back5: pd.Series,
    ret20: pd.Series,
    call_band: int = CALL_BAND,
    put_band: int = PUT_BAND,
) -> list[Signal]:
    """Signals for one session. Frames are bar-start index x ticker."""
    tickers = [t for t in c.columns if pd.notna(prev.get(t))]
    if len(tickers) < 3 or c.empty:
        return []
    o, h, lo, c = o[tickers], h[tickers], lo[tickers], c[tickers]
    prev = prev[tickers]

    day_hi = h.cummax()
    day_lo = lo.cummin()
    mid = (day_hi + day_lo) / 2
    irs = rank99((c - mid) / mid)
    r5 = rank99(c / back5.reindex(tickers) - 1)
    r20_today = rank99(ret20.reindex(tickers).to_frame().T).iloc[0]
    day_pct = (c / prev - 1) * 100
    first_open = o.bfill().iloc[0]
    gap_pct = (first_open / prev - 1) * 100

    seen: set[tuple[str, str]] = set()
    signals: list[Signal] = []
    idx = c.index
    hv, lv, cv = h.to_numpy(), lo.to_numpy(), c.to_numpy()
    irs_v = irs.to_numpy()
    for i, ts in enumerate(idx):
        label = (ts + pd.Timedelta(minutes=5)).strftime("%H:%M")
        for j, t in enumerate(tickers):
            score = irs_v[i, j]
            price = cv[i, j]
            if np.isnan(score) or np.isnan(price) or price < MIN_PRICE:
                continue
            side = "call" if score >= call_band else "put" if score <= put_band else None
            if side is None or (t, side) in seen:
                continue
            seen.add((t, side))
            fav, adv, close = _forward(side, price, hv[i + 1 :, j], lv[i + 1 :, j], cv[i + 1 :, j])
            ok = ~np.isnan(close)
            fav, adv, close = fav[ok], adv[ok], close[ok]
            if close.size == 0:
                continue  # signal on the last bar: nothing left to measure
            hit_at = np.flatnonzero(fav >= HIT_PCT)
            r5_val = r5.iat[i, j]
            signals.append(
                Signal(
                    date=str(pd.Timestamp(date).date()),
                    time=label,
                    ticker=t,
                    theme=theme_of(t),
                    side=side,
                    irs=int(score),
                    r5=int(r5_val) if pd.notna(r5_val) else -1,
                    r20=int(r20_today[t]) if pd.notna(r20_today.get(t)) else -1,
                    day_pct=round(float(day_pct.iat[i, j]), 2),
                    gap_pct=round(float(gap_pct[t]), 2) if pd.notna(gap_pct[t]) else 0.0,
                    entry=round(float(price), 4),
                    mfe=round(max(float(fav.max()), 0.0), 2),
                    mae=round(min(float(adv.min()), 0.0), 2),
                    close_ret=round(float(close[-1]), 2),
                    mins_to_hit=float((hit_at[0] + 1) * 5) if hit_at.size else None,
                    fwd_fav=fav,
                    fwd_adv=adv,
                    fwd_close=close,
                )
            )
    _add_context(signals)
    return signals


def _add_context(signals: list[Signal]) -> None:
    """Running call/put counts and same-theme cluster size, using only
    signals at or before each signal's time."""
    signals.sort(key=lambda s: (s.minutes, s.ticker))
    for s in signals:
        earlier = [x for x in signals if x.minutes <= s.minutes]
        s.calls_so_far = sum(x.side == "call" for x in earlier)
        s.puts_so_far = sum(x.side == "put" for x in earlier)
        if s.theme == "other":
            s.cluster_n = 1
        else:
            s.cluster_n = sum(
                x.side == s.side
                and x.theme == s.theme
                and s.minutes - x.minutes <= CLUSTER_WINDOW_MIN
                for x in earlier
            )


def replay(intraday: dict[str, pd.DataFrame], daily: dict[str, pd.DataFrame],
           wide_bands: bool = False) -> list[Signal]:
    """Run the scanner over every session present in the intraday bars."""
    bands = (WIDE_CALL_BAND, WIDE_PUT_BAND) if wide_bands else (CALL_BAND, PUT_BAND)
    intraday = {t: regular_session(df) for t, df in intraday.items()}
    intraday = {t: df for t, df in intraday.items() if not df.empty and t in daily}
    if not intraday:
        return []
    o, h, lo, c = (_wide(intraday, k) for k in ("Open", "High", "Low", "Close"))
    refs = _daily_refs(daily, list(intraday))
    out: list[Signal] = []
    for date in sorted(set(c.index.normalize())):
        if date not in refs["prev"].index:
            continue
        day = c.index.normalize() == date
        out.extend(
            scan_day(
                date,
                o[day], h[day], lo[day], c[day],
                refs["prev"].loc[date],
                refs["back5"].loc[date],
                refs["ret20"].loc[date],
                *bands,
            )
        )
    return out
