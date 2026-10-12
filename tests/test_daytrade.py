import json

import numpy as np
import pandas as pd
import pytest

from swingtrader.daytrade import data, filters, report, scanner
from swingtrader.daytrade.scanner import Signal


def _session(date: str, path: list[float]) -> pd.DataFrame:
    """5-minute bars from 09:30 whose closes follow `path`; each bar's
    high/low sit 0.1% around its open/close."""
    idx = pd.date_range(f"{date} 09:30", periods=len(path), freq="5min")
    close = np.array(path, dtype=float)
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * 1.001
    low = np.minimum(open_, close) * 0.999
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": 1000}, index=idx)


def _daily(last_date: str, closes: list[float]) -> pd.DataFrame:
    idx = pd.bdate_range(end=last_date, periods=len(closes))
    c = np.array(closes, dtype=float)
    return pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1e6}, index=idx)


@pytest.fixture
def market():
    """Six tickers on one session (2026-10-09), with 30 days of daily history
    ending at that session (so the prior close is the 2026-10-08 bar)."""
    n = 20
    flat = [100.0] * n
    paths = {
        "UP": list(np.linspace(100, 110, n)),     # trends up all day -> call
        "DOWN": list(np.linspace(100, 90, n)),    # trends down all day -> put
        "A": flat, "B": [100 + 0.01 * i for i in range(n)],
        "C": [100 - 0.01 * i for i in range(n)], "D": flat,
    }
    intraday = {t: _session("2026-10-09", p) for t, p in paths.items()}
    daily = {t: _daily("2026-10-09", [100.0] * 30) for t in paths}
    return intraday, daily


def test_rank99_bounds_and_nan():
    df = pd.DataFrame([[1.0, 2.0, 3.0, np.nan]])
    r = scanner.rank99(df)
    assert r.iloc[0, 0] == 0 and r.iloc[0, 2] == 99 and np.isnan(r.iloc[0, 3])


def test_replay_first_seen_signals(market):
    intraday, daily = market
    sigs = scanner.replay(intraday, daily)
    keys = [(s.ticker, s.side) for s in sigs]
    assert len(keys) == len(set(keys)), "a ticker/side fires at most once per day"
    up = next(s for s in sigs if s.ticker == "UP")
    down = next(s for s in sigs if s.ticker == "DOWN")
    assert up.side == "call" and up.band == "CALLS · iRS 95-99"
    assert down.side == "put" and down.band == "PUTS · iRS 0-5"
    assert up.time >= "09:35"


def test_outcomes_use_only_later_bars(market):
    intraday, daily = market
    up = next(s for s in scanner.replay(intraday, daily) if s.ticker == "UP")
    bars = intraday["UP"]
    sig_bar = bars.index.get_loc(pd.Timestamp(f"{up.date} {up.time}") - pd.Timedelta(minutes=5))
    after = bars.iloc[sig_bar + 1 :]
    assert up.entry == pytest.approx(bars["Close"].iloc[sig_bar], rel=1e-6)
    assert up.mfe == pytest.approx((after["High"].max() / up.entry - 1) * 100, abs=0.01)
    assert up.close_ret == pytest.approx((after["Close"].iloc[-1] / up.entry - 1) * 100, abs=0.01)
    assert up.mae <= 0 <= up.mfe


def _sig(**kw) -> Signal:
    base = dict(date="2026-10-09", time="09:40", ticker="X", theme="semis", side="call",
                irs=97, r5=80, r20=50, day_pct=1.0, gap_pct=0.0, entry=10.0,
                mfe=0.0, mae=0.0, close_ret=0.0, mins_to_hit=None)
    base.update(kw)
    return Signal(**base)


def test_simulate_stop_before_target_and_close_exit():
    s = _sig(fwd_fav=np.array([0.5, 3.0]), fwd_adv=np.array([-1.5, 0.0]), fwd_close=np.array([0, 2]))
    assert report.simulate(s, target=2, stop=1) == -1  # stop hit on bar 1 first
    s = _sig(fwd_fav=np.array([2.5]), fwd_adv=np.array([-1.5]), fwd_close=np.array([1.0]))
    assert report.simulate(s, target=2, stop=1) == -1  # same bar: assume stop
    s = _sig(fwd_fav=np.array([0.5, 1.0]), fwd_adv=np.array([-0.2, -0.5]), fwd_close=np.array([0.3, 0.7]))
    assert report.simulate(s, target=2, stop=1) == pytest.approx(0.7)


def test_context_has_no_lookahead():
    sigs = [
        _sig(ticker="A", time="09:35"),
        _sig(ticker="B", time="09:40"),
        _sig(ticker="C", time="10:00", side="put", irs=2),
        _sig(ticker="D", time="11:30"),
    ]
    scanner._add_context(sigs)
    by = {s.ticker: s for s in sigs}
    assert (by["A"].calls_so_far, by["A"].puts_so_far) == (1, 0)
    assert (by["C"].calls_so_far, by["C"].puts_so_far) == (2, 1)
    assert by["B"].cluster_n == 2           # A and B, same theme and side
    assert by["D"].cluster_n == 1           # A/B are outside the 60-min window


def test_filters():
    assert filters.gap_reversal(_sig(gap_pct=-8, irs=99, day_pct=-6))
    assert not filters.gap_reversal(_sig(gap_pct=-2, irs=99))
    assert filters.trend_agrees(_sig(side="put", r5=10)) and not filters.trend_agrees(_sig(r5=50))
    assert filters.put_off_low(_sig(side="put", irs=7)) and not filters.put_off_low(_sig(side="put", irs=1))
    assert filters.fallen_leader(_sig(side="put", irs=2, r20=90, r5=4))
    # 4 calls vs 8 puts so far: calls are the lopsided minority
    assert filters.minority_side(_sig(calls_so_far=4, puts_so_far=8))
    assert not filters.minority_side(_sig(side="put", calls_so_far=4, puts_so_far=8))
    assert not filters.minority_side(_sig(calls_so_far=5, puts_so_far=6))  # balanced
    assert not filters.early(_sig(time="11:05")) and filters.early(_sig(time="11:00"))


def test_report_is_json_serializable(market):
    intraday, daily = market
    res = report.build(scanner.replay(intraday, daily))
    assert res["sessions"] == 1 and res["signals"] > 0
    assert {r["filter"] for r in res["filters"]} >= {"all", "core"}
    json.dumps(res)


def test_load_bars_merges_into_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "CACHE_DIR", tmp_path)
    old = _session("2026-10-08", [100, 101])
    new = _session("2026-10-09", [102, 103])
    old.to_pickle(tmp_path / "XYZ_5m.pkl")
    # make the existing file look stale so a download happens
    import os
    os.utime(tmp_path / "XYZ_5m.pkl", (0, 0))
    monkeypatch.setattr(data, "_download", lambda t, i, p: {"XYZ": new})
    out = data.load_bars(["XYZ"], "5m")
    assert len(out["XYZ"]) == 4
    assert pd.read_pickle(tmp_path / "XYZ_5m.pkl").index.min() == old.index.min()


def test_split_batch_multiindex():
    a = _session("2026-10-09", [1, 2])
    raw = pd.concat({"AAA": a, "BBB": a}, axis=1)
    out = data._split_batch(raw, ["AAA", "BBB", "CCC"])
    assert set(out) == {"AAA", "BBB"} and list(out["AAA"].columns) == list(a.columns)


def test_band_thresholds(market):
    intraday, daily = market
    default = scanner.replay(intraday, daily)
    wide = scanner.replay(intraday, daily, wide_bands=True)
    assert all(s.irs >= 95 or s.irs <= 5 for s in default)
    assert len(wide) >= len(default)


def test_webapp_daytrade_routes(market, monkeypatch):
    import webapp
    from swingtrader import daytrade

    intraday, daily = market
    monkeypatch.setattr(
        daytrade.data, "load_bars",
        lambda tickers, interval="5m", refresh=True: intraday if interval == "5m" else daily,
    )
    client = webapp.app.test_client()
    assert client.get("/daytrade").status_code == 200
    assert b'href="/daytrade"' in client.get("/").data
    res = client.post("/api/daytrade/backtest", json={"cached": True, "target": 1.5, "stop": 0.5})
    body = res.get_json()
    assert res.status_code == 200 and body["signals"] > 0 and "_signals" not in body
    assert body["target_pct"] == 1.5 and body["stop_pct"] == 0.5
    assert client.post("/api/daytrade/backtest", json={"universe": "custom", "tickers": " "}).status_code == 400
    assert client.post("/api/daytrade/backtest", json={"stop": "abc"}).status_code == 400
