"""Backtest statistics for replayed signals: the EOD-style RS profile, plus
filter comparisons and a simple target/stop exit simulation."""

from collections import defaultdict

import numpy as np

from .filters import FILTERS
from .scanner import HIT_PCT, Signal

TARGET_PCT = 2.0
STOP_PCT = 1.0


def simulate(s: Signal, target: float = TARGET_PCT, stop: float = STOP_PCT) -> float:
    """Exit at +target or -stop, else at the close. When one bar touches
    both, assume the stop filled first (conservative)."""
    for fav, adv in zip(s.fwd_fav, s.fwd_adv):
        if adv <= -stop:
            return -stop
        if fav >= target:
            return target
    return float(s.fwd_close[-1])


def stats(signals: list[Signal], target: float = TARGET_PCT, stop: float = STOP_PCT) -> dict:
    n = len(signals)
    if n == 0:
        return {"n": 0}
    pnl = np.array([simulate(s, target, stop) for s in signals])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_loss = -losses.sum()
    return {
        "n": n,
        "hit_pct": round(100 * np.mean([s.hit(HIT_PCT) for s in signals]), 1),
        "avg_mfe": round(float(np.mean([s.mfe for s in signals])), 2),
        "avg_mae": round(float(np.mean([s.mae for s in signals])), 2),
        "avg_close": round(float(np.mean([s.close_ret for s in signals])), 2),
        "sim_win_pct": round(100 * len(wins) / n, 1),
        "sim_expectancy": round(float(pnl.mean()), 3),
        "sim_profit_factor": round(float(wins.sum() / gross_loss), 2) if gross_loss > 0 else None,
    }


def _grouped(signals: list[Signal], key, target, stop) -> list[dict]:
    groups: dict[str, list[Signal]] = defaultdict(list)
    for s in signals:
        groups[key(s)].append(s)
    return [{"group": g, **stats(v, target, stop)} for g, v in sorted(groups.items())]


def _hour_bucket(s: Signal) -> str:
    if s.minutes <= 10 * 60:
        return "09:35-10:00"
    if s.minutes <= 11 * 60:
        return "10:05-11:00"
    if s.minutes <= 13 * 60:
        return "11:05-13:00"
    return "13:05-15:55"


def _daily(signals: list[Signal]) -> list[dict]:
    by_day: dict[str, list[Signal]] = defaultdict(list)
    for s in signals:
        by_day[s.date].append(s)
    rows = []
    for date, day in sorted(by_day.items()):
        calls = [s for s in day if s.side == "call"]
        puts = [s for s in day if s.side == "put"]
        rows.append(
            {
                "date": date,
                "calls": len(calls),
                "puts": len(puts),
                "call_hit_pct": round(100 * np.mean([s.hit() for s in calls]), 1) if calls else None,
                "put_hit_pct": round(100 * np.mean([s.hit() for s in puts]), 1) if puts else None,
                "top_runners": [
                    s.to_dict() for s in sorted(day, key=lambda s: s.mfe, reverse=True)[:8]
                ],
            }
        )
    return rows


def build(signals: list[Signal], filters: list[str] | None = None,
          target: float = TARGET_PCT, stop: float = STOP_PCT) -> dict:
    names = [f for f in (filters or FILTERS) if f in FILTERS]
    if "all" not in names:
        names.insert(0, "all")

    filter_rows = []
    for name in names:
        label, pred = FILTERS[name]
        chosen = [s for s in signals if pred(s)]
        for side in ("call", "put"):
            picked = [s for s in chosen if s.side == side]
            if picked or name == "all":
                filter_rows.append({"filter": name, "label": label, "side": side, **stats(picked, target, stop)})

    return {
        "sessions": len({s.date for s in signals}),
        "signals": len(signals),
        "target_pct": target,
        "stop_pct": stop,
        "hit_threshold_pct": HIT_PCT,
        "rs_profile": _grouped(signals, lambda s: s.band, target, stop),
        "by_time": _grouped(signals, lambda s: f"{s.side.upper()}S · {_hour_bucket(s)}", target, stop),
        "filters": filter_rows,
        "daily": _daily(signals),
    }
