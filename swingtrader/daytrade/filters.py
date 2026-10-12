"""Signal filters from the screenshot deep dive. Each one only uses data
known at the signal's time. Thresholds are module constants so they can be
tuned after a backtest."""

from typing import Callable

from .scanner import Signal

EARLY_CUTOFF = 11 * 60          # 11:00 ET
CALL_TREND_MIN = 75             # 5D rank a call needs
PUT_TREND_MAX = 15              # 5D rank a put needs
GAP_REVERSAL_PCT = -5.0         # gap down at least this much
CLUSTER_MIN = 3                 # same theme, same side, within the window
IMBALANCE_RATIO = 1.25          # majority / minority signal count
IMBALANCE_MIN_SIGNALS = 10      # running total before the ratio counts
LEADER_R20_MIN = 75
LEADER_R5_MAX = 10


def early(s: Signal) -> bool:
    return s.minutes <= EARLY_CUTOFF


def day_agrees(s: Signal) -> bool:
    return s.day_pct > 0 if s.side == "call" else s.day_pct < 0


def trend_agrees(s: Signal) -> bool:
    if s.r5 < 0:
        return False
    return s.r5 >= CALL_TREND_MIN if s.side == "call" else s.r5 <= PUT_TREND_MAX


def gap_reversal(s: Signal) -> bool:
    """Gapped down hard but sitting at the top of today's range."""
    return s.side == "call" and s.gap_pct <= GAP_REVERSAL_PCT and s.irs >= 95


def gap_fade(s: Signal) -> bool:
    """Mirror of gap_reversal for puts: gapped up hard, now at the lows."""
    return s.side == "put" and s.gap_pct >= -GAP_REVERSAL_PCT and s.irs <= 5


def cluster(s: Signal) -> bool:
    return s.cluster_n >= CLUSTER_MIN


def minority_side(s: Signal) -> bool:
    """The side with fewer signals so far, when the count is lopsided."""
    calls, puts = s.calls_so_far, s.puts_so_far
    if calls + puts < IMBALANCE_MIN_SIGNALS or min(calls, puts) == 0:
        return False
    if max(calls, puts) / min(calls, puts) < IMBALANCE_RATIO:
        return False
    minority = "call" if calls < puts else "put"
    return s.side == minority


def put_off_low(s: Signal) -> bool:
    """Most useful with wide bands, which record puts up to iRS 10."""
    return s.side == "put" and 3 <= s.irs <= 10


def fallen_leader(s: Signal) -> bool:
    return s.side == "put" and s.r20 >= LEADER_R20_MIN and 0 <= s.r5 <= LEADER_R5_MAX


def core(s: Signal) -> bool:
    """Momentum calls/puts with time, day and week all agreeing."""
    return early(s) and day_agrees(s) and trend_agrees(s)


FILTERS: dict[str, tuple[str, Callable[[Signal], bool]]] = {
    "all": ("All signals (baseline)", lambda s: True),
    "early": ("First seen by 11:00", early),
    "day_agrees": ("Day % agrees with side", day_agrees),
    "trend_agrees": ("5D agrees (calls >= 75, puts <= 15)", trend_agrees),
    "cluster": ("Sector cluster (3+ same theme, 60 min)", cluster),
    "minority_side": ("Minority side of a lopsided count", minority_side),
    "gap_reversal": ("Gap-down reversal calls (gap <= -5%, iRS >= 95)", gap_reversal),
    "gap_fade": ("Gap-up fade puts (gap >= +5%, iRS <= 5)", gap_fade),
    "put_off_low": ("Puts off the low (iRS 3-10)", put_off_low),
    "fallen_leader": ("Fallen-leader puts (20D >= 75, 5D <= 10)", fallen_leader),
    "core": ("Core: early + day + 5D agree", core),
    "core_cluster": ("Core + sector cluster", lambda s: core(s) and cluster(s)),
    "core_minority": ("Core + minority side", lambda s: core(s) and minority_side(s)),
}
