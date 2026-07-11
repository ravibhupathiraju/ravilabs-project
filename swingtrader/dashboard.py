"""Self-contained HTML dashboard comparing strategy backtest results."""

import json
from datetime import datetime, timezone
from string import Template

from .backtest import Results

TEMPLATE = Template("""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Swing Strategy Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; margin: 2rem; background: #0f1420; color: #e6e9f0; }
h1 { margin-bottom: 0.2rem; }
.meta { color: #9aa3b5; margin-top: 0; }
table { border-collapse: collapse; width: 100%; margin: 1.5rem 0; }
th, td { padding: 8px 12px; text-align: right; border-bottom: 1px solid #263042; }
th:first-child, td:first-child { text-align: left; }
td:first-child { font-weight: 600; }
th { color: #9aa3b5; text-transform: uppercase; font-size: 0.75rem; letter-spacing: 0.05em; }
.charts { display: grid; grid-template-columns: 1fr 1fr; gap: 2rem; margin: 2rem 0; }
.card { background: #171f30; border-radius: 10px; padding: 1rem; }
.note { color: #9aa3b5; font-size: 0.85rem; }
</style>
</head>
<body>
<h1>Swing Strategy Dashboard</h1>
<p class="meta">$meta &middot; generated $generated UTC</p>
<table>
<thead><tr><th>Strategy</th><th>Trades</th><th>Win rate</th><th>Avg win</th><th>Avg loss</th><th>Expectancy</th><th>Profit factor</th><th>Avg hold</th><th>Max DD</th></tr></thead>
<tbody>
$rows
</tbody>
</table>
<div class="charts">
<div class="card"><canvas id="wr"></canvas></div>
<div class="card"><canvas id="ex"></canvas></div>
</div>
<h3>Exit reason breakdown</h3>
<table><thead><tr><th>Strategy</th><th>Exit reasons</th></tr></thead><tbody>
$reason_rows
</tbody></table>
<p class="note">Entries are skipped and open positions closed within the earnings buffer window. Sorted by expectancy. Educational use only; not financial advice.</p>
<script>
const labels = $labels;
const common = { responsive: true, plugins: { legend: { display: false } }, scales: { x: { ticks: { color: "#9aa3b5" } }, y: { ticks: { color: "#9aa3b5" } } } };
new Chart(document.getElementById("wr"), { type: "bar", data: { labels, datasets: [{ data: $win_rates, backgroundColor: "#4f8ef7" }] }, options: { ...common, plugins: { ...common.plugins, title: { display: true, text: "Win rate (%)", color: "#e6e9f0" } } } });
new Chart(document.getElementById("ex"), { type: "bar", data: { labels, datasets: [{ data: $expectancies, backgroundColor: "#38c793" }] }, options: { ...common, plugins: { ...common.plugins, title: { display: true, text: "Expectancy per trade (%)", color: "#e6e9f0" } } } });
</script>
</body>
</html>
""")


def generate(results: dict[str, Results], path: str = "dashboard.html", meta: str = "") -> str:
    """Write the dashboard HTML file and return its path."""
    stats = {name: res.stats() for name, res in results.items()}
    order = sorted(stats.items(), key=lambda kv: kv[1].get("expectancy", float("-inf")), reverse=True)

    rows, reason_rows, labels, win_rates, expectancies = [], [], [], [], []
    for name, s in order:
        label = results[name].strategy
        if s["trades"] == 0:
            rows.append(f"<tr><td>{label}</td><td colspan='8'>no trades</td></tr>")
            continue
        rows.append(
            "<tr>"
            f"<td>{label}</td><td>{s['trades']}</td><td>{s['win_rate']:.1f}%</td>"
            f"<td>{s['avg_win']:+.2f}%</td><td>{s['avg_loss']:+.2f}%</td>"
            f"<td>{s['expectancy']:+.2f}%</td><td>{s['profit_factor']:.2f}</td>"
            f"<td>{s['avg_hold']:.1f}d</td><td>{s['max_drawdown']:.1f}%</td>"
            "</tr>"
        )
        labels.append(label)
        win_rates.append(round(s["win_rate"], 1))
        expectancies.append(round(s["expectancy"], 2))
        reasons = ", ".join(f"{k}: {v}" for k, v in s["exit_reasons"].most_common())
        reason_rows.append(f"<tr><td>{label}</td><td>{reasons}</td></tr>")

    html = TEMPLATE.substitute(
        meta=meta,
        generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        rows="\n".join(rows),
        reason_rows="\n".join(reason_rows),
        labels=json.dumps(labels),
        win_rates=json.dumps(win_rates),
        expectancies=json.dumps(expectancies),
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path
