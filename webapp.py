"""Swing Trade Tracker web UI.

Run with:
    python webapp.py
then open http://127.0.0.1:5000 in a browser.
"""

import re

from flask import Flask, jsonify, render_template, request

from swingtrader import backtest, daytrade, screener, zerodte
from swingtrader.data import parse_duration
from swingtrader.daytrade.filters import FILTERS as DAYTRADE_FILTERS
from swingtrader.daytrade.report import STOP_PCT, TARGET_PCT
from swingtrader.daytrade.universe import UNIVERSE as DAYTRADE_UNIVERSE
from swingtrader.strategies import STRATEGIES
from swingtrader.universe import get_universe, universe_options

app = Flask(__name__)


def _tickers_from(payload: dict) -> list[str]:
    universe = payload.get("universe", "watchlist")
    if universe == "custom":
        raw = payload.get("tickers", "")
        tickers = [t.upper() for t in re.split(r"[,\s]+", raw) if t.strip()]
    else:
        tickers = get_universe(universe)
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


# --- Day-trade scanner: separate page, separate API ---


@app.route("/daytrade")
def daytrade_page():
    return render_template(
        "daytrade.html",
        universe_size=len(DAYTRADE_UNIVERSE),
        target=TARGET_PCT,
        stop=STOP_PCT,
        filters=[{"key": k, "label": label} for k, (label, _) in DAYTRADE_FILTERS.items()],
    )


@app.post("/api/daytrade/backtest")
def api_daytrade_backtest():
    payload = request.get_json(force=True)
    if payload.get("universe") == "custom":
        raw = payload.get("tickers", "")
        tickers = [t.upper() for t in re.split(r"[,\s]+", raw) if t.strip()]
        if not tickers:
            return jsonify({"error": "no tickers entered"}), 400
    else:
        tickers = list(DAYTRADE_UNIVERSE)
    limit = int(payload.get("limit") or 0)
    if limit > 0:
        tickers = tickers[:limit]
    try:
        target = float(payload.get("target") or TARGET_PCT)
        stop = float(payload.get("stop") or STOP_PCT)
    except (TypeError, ValueError):
        return jsonify({"error": "take profit and stop must be numbers"}), 400
    if target <= 0 or stop <= 0:
        return jsonify({"error": "take profit and stop must be positive"}), 400
    res = daytrade.run_backtest(
        tickers,
        filters=payload.get("filters") or None,
        target=target,
        stop=stop,
        refresh=not payload.get("cached"),
        wide_bands=bool(payload.get("wide_bands")),
    )
    res.pop("_signals", None)
    return jsonify(res)


if __name__ == "__main__":
    app.run(debug=True)
