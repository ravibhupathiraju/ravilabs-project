"""Swing Trade Tracker web UI.

Run with:
    python webapp.py
then open http://127.0.0.1:5000 in a browser.
"""

import re

from flask import Flask, jsonify, render_template, request

from swingtrader import (
    actionplan, alerts, analyzer, backtest, bear, broker, fibdiv, patterns,
    perf, screener, tracker, tvtester, zerodte,
)
from swingtrader.data import parse_duration
from swingtrader.strategies import STRATEGIES
from swingtrader.universe import get_universe, universe_options

app = Flask(__name__)


def _tickers_from(payload: dict) -> list[str]:
    universe = payload.get("universe", "watchlist")
    raw = payload.get("tickers", "")
    custom = [t.upper() for t in re.split(r"[,\s]+", raw) if t.strip()]
    if universe == "custom":
        tickers = custom
    else:
        tickers = get_universe(universe)
        if payload.get("include_custom"):  # extra tickers on top of a universe
            seen = set(tickers)
            tickers += [t for t in custom if t not in seen]
    limit = int(payload.get("limit") or 0)
    if limit > 0:
        tickers = tickers[:limit]
    return tickers


def _strategies_from(payload: dict) -> list:
    names = payload.get("strategies") or []
    chosen = [STRATEGIES[n] for n in names if n in STRATEGIES]
    return chosen or list(STRATEGIES.values())


@app.route("/")
def index():
    return render_template(
        "index.html",
        universes=universe_options(),
        strategies=[
            {"name": s.name, "label": s.label, "description": s.description}
            for s in STRATEGIES.values()
        ],
    )


@app.post("/api/backtest")
def api_backtest():
    payload = request.get_json(force=True)
    try:
        duration_days = parse_duration(payload.get("duration", "1y"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    tickers = _tickers_from(payload)
    if not tickers:
        return jsonify({"error": "no tickers selected"}), 400
    strategies = _strategies_from(payload)
    buffer_days = int(payload.get("earnings_buffer", 3))
    results = backtest.run_all(
        tickers, strategies, duration_days=duration_days, earnings_buffer=buffer_days
    )
    rows = []
    for res in results.values():
        s = res.stats()
        s["strategy"] = res.strategy
        if s["trades"]:
            s["exit_reasons"] = dict(s["exit_reasons"])
            # JSON cannot carry infinity; None renders as the infinity symbol.
            if s["profit_factor"] == float("inf"):
                s["profit_factor"] = None
            s["trade_list"] = [
                {
                    "ticker": t.ticker,
                    "entry_date": t.entry_date,
                    "exit_date": t.exit_date,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "days_held": t.days_held,
                    "exit_reason": t.exit_reason,
                    "pnl_pct": t.pnl_pct,
                }
                for t in sorted(res.trades, key=lambda t: t.entry_date, reverse=True)
            ]
        rows.append(s)
    rows.sort(key=lambda s: s.get("expectancy", float("-inf")), reverse=True)
    return jsonify(
        {
            "results": rows,
            "tickers_used": len(tickers),
            "duration_days": duration_days,
            "earnings_buffer": buffer_days,
        }
    )


@app.post("/api/scan")
def api_scan():
    payload = request.get_json(force=True)
    tickers = _tickers_from(payload)
    if not tickers:
        return jsonify({"error": "no tickers selected"}), 400
    strategies = _strategies_from(payload)
    buffer_days = int(payload.get("earnings_buffer", 3))
    signals = screener.scan(tickers, strategies, earnings_buffer=buffer_days)
    return jsonify({"signals": signals, "tickers_used": len(tickers)})


@app.post("/api/zerodte")
def api_zerodte():
    payload = request.get_json(force=True)
    symbols = payload.get("symbols") or list(zerodte.SYMBOLS)
    strategies = payload.get("strategies") or list(zerodte.STRATEGY_LABELS)
    try:
        data = zerodte.run(
            symbols,
            strategies,
            start=payload.get("start") or None,
            end=payload.get("end") or None,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(data)


@app.post("/api/alerts/start")
def api_alerts_start():
    payload = request.get_json(force=True)
    # Each tab configures only its own channel: the 0DTE tab sends
    # configure=["zerodte"], the swing/bear tabs send ["swing"]. Starting one
    # therefore leaves the other's config (and alerts) running untouched.
    configure = tuple(payload.get("configure") or ("zerodte", "swing"))
    buffer_raw = payload.get("earnings_buffer")
    return jsonify(
        alerts.MONITOR.start(
            payload.get("symbols") or [],
            payload.get("strategies") or [],
            swing_universe=payload.get("swing_universe"),
            swing_strategies=payload.get("swing_strategies") or [],
            swing_tickers=payload.get("swing_tickers") or [],
            earnings_buffer=int(buffer_raw) if buffer_raw not in (None, "") else None,
            reversal_tickers=payload.get("reversal_tickers") or [],
            pattern_on=payload.get("pattern_on"),
            configure=configure,
        )
    )


@app.post("/api/alerts/stop")
def api_alerts_stop():
    return jsonify(alerts.MONITOR.stop())


@app.get("/api/alerts/status")
def api_alerts_status():
    return jsonify(alerts.MONITOR.status())


@app.get("/api/alerts/history")
def api_alerts_history():
    symbols = [s for s in request.args.get("symbols", "").split(",") if s]
    rows = alerts.history(
        request.args.get("start") or None,
        request.args.get("end") or None,
        symbols or None,
        kind=request.args.get("kind") or None,
        strategy=request.args.get("strategy") or None,
    )
    return jsonify({"alerts": rows})


@app.get("/api/alerts/performance")
def api_alerts_performance():
    return jsonify(
        alerts.performance(
            strategy=request.args.get("strategy") or None,
            kind=request.args.get("kind") or None,
        )
    )


@app.get("/api/broker/status")
def api_broker_status():
    return jsonify(broker.status())


@app.get("/api/settings/toasts")
def api_toasts_get():
    return jsonify({"toasts": alerts.toasts_enabled()})


@app.post("/api/settings/toasts")
def api_toasts_set():
    payload = request.get_json(force=True)
    return jsonify({"toasts": alerts.set_toasts(bool(payload.get("on")))})


@app.get("/api/broker/channels")
def api_broker_channels():
    return jsonify({"channels": broker.channels()})


@app.post("/api/broker/channels")
def api_broker_channels_set():
    payload = request.get_json(force=True)
    try:
        ch = broker.set_channel(str(payload.get("channel")), bool(payload.get("active")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"channels": ch})


@app.post("/api/tv/parse")
def api_tv_parse():
    payload = request.get_json(force=True)
    return jsonify(tvtester.parse(payload.get("notes") or ""))


@app.post("/api/tv/plan")
def api_tv_plan():
    payload = request.get_json(force=True)
    try:
        res = tvtester.arm_plan(
            ticker=payload.get("ticker") or "",
            qty=int(payload.get("qty") or 0),
            entry=float(payload.get("entry") or 0),
            stop=float(payload.get("stop") or 0),
            target=float(payload.get("target") or 0),
            notes=payload.get("notes") or "",
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return (jsonify(res), 400) if "error" in res else jsonify(res)


@app.post("/api/tv/plan/cancel")
def api_tv_plan_cancel():
    payload = request.get_json(force=True)
    return jsonify(tvtester.cancel_plan(int(payload.get("id"))))


@app.post("/api/tv/manual")
def api_tv_manual():
    payload = request.get_json(force=True)
    try:
        res = tvtester.manual_order(
            ticker=payload.get("ticker") or "",
            side=payload.get("side") or "",
            qty=int(payload.get("qty") or 0),
            limit=float(payload["limit"]) if payload.get("limit") else None,
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return (jsonify(res), 400) if "error" in res else jsonify(res)


@app.get("/api/tv/summary")
def api_tv_summary():
    return jsonify(tvtester.summary())


@app.get("/api/tv/autopick")
def api_tv_autopick_status():
    from swingtrader.alerts import _settings
    s = _settings()
    return jsonify({
        "enabled": bool(s.get("autopick", True)),
        "last_date": s.get("last_autopick_date"),
        "last": s.get("autopick_last"),
    })


@app.post("/api/tv/autopick")
def api_tv_autopick():
    """Toggle the morning picker, or run it now ({"run": true, "dry_run": bool})."""
    from swingtrader import autopick
    from swingtrader.alerts import _settings, _settings_set

    payload = request.get_json(force=True)
    if "enabled" in payload:
        _settings_set("autopick", bool(payload["enabled"]))
    if payload.get("run"):
        result = autopick.run(dry_run=bool(payload.get("dry_run")))
        _settings_set("autopick_last", result)
        if "error" not in result and not result.get("dry_run"):
            _settings_set("last_autopick_date",
                          result["ran_at"][:10])
        return jsonify({"enabled": bool(_settings().get("autopick", True)),
                        "last": result})
    return jsonify({"enabled": bool(_settings().get("autopick", True)),
                    "last": _settings().get("autopick_last")})


def _laptop_config() -> dict:
    """Current unattended-trading power settings + live machine status."""
    import sys
    import subprocess
    from swingtrader.alerts import MONITOR, _settings, _idle_seconds

    s = _settings()
    task = None  # True/False = task present/absent; None = unknown (non-Windows/err)
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["schtasks", "/query", "/tn", "SwingTracker-AutoTrade"],
                capture_output=True, text=True, timeout=5,
            )
            task = r.returncode == 0
        except Exception:
            task = None
    return {
        "is_windows": sys.platform == "win32",
        "hibernate_after_close": bool(s.get("hibernate_after_close", True)),
        "wakelock_enabled": bool(s.get("wakelock_enabled", True)),
        "monitor_running": MONITOR.running,
        "wakelock_on": MONITOR._wakelock_on,
        "hibernated_date": MONITOR._hibernated_date,
        "idle_seconds": round(_idle_seconds()),
        "scheduled_task": task,
        "started_at": MONITOR.started_at,
    }


@app.get("/api/laptop/config")
def api_laptop_config():
    return jsonify(_laptop_config())


@app.post("/api/laptop/config")
def api_laptop_config_set():
    from swingtrader.alerts import _settings_set
    payload = request.get_json(force=True)
    for key in ("hibernate_after_close", "wakelock_enabled"):
        if key in payload:
            _settings_set(key, bool(payload[key]))
    return jsonify(_laptop_config())


@app.get("/api/zerodte/signals")
def api_zerodte_signals():
    start = (request.args.get("start") or "").strip()
    if not start:
        return jsonify({"error": "start date required"}), 400
    syms = [s for s in re.split(r"[,\s]+", request.args.get("symbols") or "") if s.strip()]
    return jsonify(alerts.zerodte_signals(
        start, (request.args.get("end") or "").strip() or None, syms))


@app.get("/api/actionplan")
def api_actionplan_list():
    return jsonify(actionplan.list_entries(request.args.get("date") or None))


@app.post("/api/actionplan/add")
def api_actionplan_add():
    payload = request.get_json(force=True)
    try:
        data = actionplan.add(
            date=payload.get("date"),
            note=payload.get("note") or "",
            plan=payload.get("plan") or "",
            status=payload.get("status"),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(data)


@app.post("/api/actionplan/update")
def api_actionplan_update():
    payload = request.get_json(force=True)
    return jsonify(actionplan.update(
        int(payload.get("id")),
        date=payload.get("date"),
        note=payload.get("note"),
        plan=payload.get("plan"),
        status=payload.get("status"),
    ))


@app.post("/api/actionplan/delete")
def api_actionplan_delete():
    payload = request.get_json(force=True)
    return jsonify(actionplan.delete(int(payload.get("id"))))


@app.post("/api/perf")
def api_perf():
    payload = request.get_json(force=True)
    raw = payload.get("symbols") or ""
    symbols = [t for t in re.split(r"[,\s]+", raw) if t.strip()]
    return jsonify(perf.analyze(
        channels=payload.get("channels") or None,
        start=payload.get("start") or None,
        end=payload.get("end") or None,
        symbols=symbols or None,
        strategy=payload.get("strategy") or None,
    ))


@app.post("/api/analyze")
def api_analyze():
    payload = request.get_json(force=True)
    raw = payload.get("tickers", "")
    tickers = [t.upper() for t in re.split(r"[,\s]+", raw) if t.strip()]
    if not tickers:
        return jsonify({"error": "enter at least one ticker"}), 400
    if len(tickers) > 20:
        return jsonify({"error": "20 tickers max per analysis"}), 400
    return jsonify({"results": analyzer.analyze(tickers)})


@app.get("/api/analyze/chart")
def api_analyze_chart():
    ticker = (request.args.get("ticker") or "").upper().strip()
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    tf = (request.args.get("tf") or "1d").lower()
    return jsonify(analyzer.chart_data(ticker, tf=tf))


@app.get("/api/analyze/rsi-pattern")
def api_analyze_rsi_pattern():
    ticker = (request.args.get("ticker") or "").upper().strip()
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    tf = (request.args.get("tf") or "1d").lower()
    return jsonify(analyzer.rsi_pattern(ticker, tf=tf))


@app.post("/api/patterns/run")
def api_patterns_run():
    payload = request.get_json(force=True)
    try:
        data = patterns.run(
            ticker=(payload.get("ticker") or "QQQ").upper().strip(),
            lookback_days=int(payload.get("lookback_days") or 365),
            target=float(payload.get("target") or 1.0),
            interval_min=int(payload.get("interval_min") or 30),
            min_support=int(payload.get("min_support") or 40),
            force=bool(payload.get("force")),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(data)


@app.get("/api/misc/fibdiv")
def api_misc_fibdiv():
    ticker = (request.args.get("ticker") or "").upper().strip()
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    return jsonify(fibdiv.analyze(ticker))


@app.post("/api/misc/scan")
def api_misc_scan():
    payload = request.get_json(force=True)
    tickers = _tickers_from(payload)
    if not tickers:
        return jsonify({"error": "no tickers selected"}), 400
    only_div = payload.get("only_divergence", True)
    rows = fibdiv.scan(tickers, only_divergence=bool(only_div))
    return jsonify({"results": rows, "tickers_scanned": len(tickers)})


@app.get("/api/misc/tracker")
def api_misc_tracker():
    return jsonify(tracker.list_all())


@app.post("/api/misc/tracker/add")
def api_misc_tracker_add():
    payload = request.get_json(force=True)
    return jsonify(tracker.add(payload.get("items") or []))


@app.post("/api/misc/tracker/delete")
def api_misc_tracker_delete():
    payload = request.get_json(force=True)
    return jsonify(tracker.delete(int(payload.get("id")), payload.get("bucket") or "active"))


@app.post("/api/misc/tracker/archive")
def api_misc_tracker_archive():
    payload = request.get_json(force=True)
    return jsonify(tracker.archive(int(payload.get("id")), payload.get("note") or ""))


@app.post("/api/patterns/arm")
def api_patterns_arm():
    payload = request.get_json(force=True)
    try:
        cfg = patterns.arm(
            ticker=(payload.get("ticker") or "QQQ").upper().strip(),
            lookback_days=int(payload.get("lookback_days") or 365),
            target=float(payload.get("target") or 1.0),
            interval_min=int(payload.get("interval_min") or 30),
            min_support=int(payload.get("min_support") or 40),
            min_win=float(payload.get("min_win") or 70.0),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(cfg)


@app.get("/api/patterns/armed")
def api_patterns_armed():
    return jsonify(patterns.live_status(request.args.get("ticker") or None))


@app.get("/api/patterns/cache")
def api_patterns_cache():
    return jsonify({"studies": patterns.cached_studies()})


@app.get("/api/patterns/backtest")
def api_patterns_backtest():
    start = (request.args.get("start") or "").strip()
    if not start:
        return jsonify({"error": "start date required"}), 400
    try:
        data = patterns.backtest(
            start=start,
            end=(request.args.get("end") or "").strip() or None,
            ticker=(request.args.get("ticker") or "").strip() or None,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(data)


@app.post("/api/patterns/disarm")
def api_patterns_disarm():
    payload = request.get_json(silent=True) or {}
    patterns.disarm(payload.get("ticker") or None)
    return jsonify(patterns.live_status())


@app.post("/api/bear/scan")
def api_bear_scan():
    payload = request.get_json(force=True)
    tickers = _tickers_from(payload)
    if not tickers:
        return jsonify({"error": "no tickers selected"}), 400
    return jsonify(
        bear.scan_rank(
            tickers,
            top=int(payload.get("top", bear.TOP_N)),
            min_score=int(payload.get("min_score") or 0),
        )
    )


@app.post("/api/bear/research")
def api_bear_research():
    payload = request.get_json(force=True)
    tickers = _tickers_from(payload)
    if not tickers:
        return jsonify({"error": "no tickers selected"}), 400
    try:
        data = bear.research(
            tickers,
            payload.get("start"),
            payload.get("end"),
            top=int(payload.get("top", bear.TOP_N)),
            min_score=int(payload.get("min_score") or 0),
        )
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(data)


def _autostart_monitors() -> None:
    """Bring every strategy live on boot: 0DTE + swing monitors polling
    during market hours, ready to place paper trades when signals trigger.

    TV-tester plans need no monitor -- their brackets rest server-side at
    Alpaca. Defaults: all 0DTE symbols/strategies, swing over the full
    universe with every strategy, standard earnings buffer. Starting from
    a tab afterwards just reconfigures that tab's channel.
    """
    try:
        alerts.MONITOR.start(
            symbols=list(zerodte.SYMBOLS),
            strategies=[],  # empty = all 0DTE strategies
            swing_universe="all",
            swing_strategies=list(STRATEGIES),
            configure=("zerodte", "swing"),
        )
        print("[autostart] monitors live: 0DTE + swing (all strategies), "
              "paper trading ready on their own accounts")
    except Exception as exc:
        print(f"[autostart] monitor failed: {exc}")


if __name__ == "__main__":
    import os

    # With the debug reloader, only the serving child (WERKZEUG_RUN_MAIN set)
    # may start the monitor thread -- otherwise it runs twice.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        _autostart_monitors()
    app.run(debug=True)
