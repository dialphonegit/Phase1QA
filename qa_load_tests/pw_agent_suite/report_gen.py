"""Generate a self-contained HTML report from the SQLite database."""
import time
import json
from pathlib import Path
import db


_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>PW Agent Suite — Report {ts}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  body{{font-family:Inter,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:24px}}
  h1{{font-size:1.4rem;font-weight:700;margin-bottom:4px}}
  .sub{{color:#64748b;font-size:.85rem;margin-bottom:24px}}
  .kpi-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-bottom:28px}}
  .kpi{{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:16px}}
  .kpi .val{{font-size:2rem;font-weight:700;font-family:monospace}}
  .kpi .lbl{{font-size:.75rem;color:#94a3b8;margin-top:4px}}
  .green{{color:#4ade80}}.blue{{color:#60a5fa}}.yellow{{color:#fbbf24}}.purple{{color:#c084fc}}
  table{{width:100%;border-collapse:collapse;font-size:.8rem;background:#1e293b;border-radius:12px;overflow:hidden}}
  th{{background:#0f172a;color:#94a3b8;padding:10px 14px;text-align:left;font-weight:500}}
  td{{padding:9px 14px;border-top:1px solid #334155}}
  tr:hover td{{background:#263348}}
  .badge{{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.7rem;font-weight:600}}
  .badge-ok{{background:#14532d;color:#4ade80}}.badge-miss{{background:#450a0a;color:#f87171}}
  .charts{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:24px 0}}
  .chart-box{{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:20px}}
  h2{{font-size:1rem;font-weight:600;margin:24px 0 12px}}
  @media(max-width:700px){{.charts{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<h1>PW Agent Suite — Load Test Report</h1>
<div class="sub">Generated {ts} &nbsp;·&nbsp; DialPhone QA &nbsp;·&nbsp; portal.qa.unifiedrtc.com</div>

<div class="kpi-grid">
  <div class="kpi"><div class="val green">{answered}</div><div class="lbl">Calls Answered</div></div>
  <div class="kpi"><div class="val" style="color:#f87171">{missed}</div><div class="lbl">Calls Missed</div></div>
  <div class="kpi"><div class="val yellow">{avg_dur}s</div><div class="lbl">Avg Hold Duration</div></div>
  <div class="kpi"><div class="val purple">{total_dur}</div><div class="lbl">Total Talk Time</div></div>
  <div class="kpi"><div class="val blue">{agents}</div><div class="lbl">Agents Participated</div></div>
  <div class="kpi"><div class="val">{asr}%</div><div class="lbl">Answer Rate</div></div>
</div>

<div class="charts">
  <div class="chart-box"><div style="font-weight:600;margin-bottom:12px">Call Volume Timeline</div>
    <canvas id="c1"></canvas></div>
  <div class="chart-box"><div style="font-weight:600;margin-bottom:12px">Outcome Breakdown</div>
    <canvas id="c2"></canvas></div>
</div>

<h2>Top Agents by Calls Answered</h2>
<table>
  <thead><tr><th>#</th><th>Agent</th><th>Extension</th><th>Answered</th><th>Missed</th><th>Avg Dur</th><th>Total</th></tr></thead>
  <tbody>{agent_rows}</tbody>
</table>

<h2>Recent Calls (last 200)</h2>
<table>
  <thead><tr><th>Time</th><th>Agent</th><th>Ext</th><th>Outcome</th><th>Duration</th></tr></thead>
  <tbody>{call_rows}</tbody>
</table>

<script>
const tl = {timeline_json};
new Chart(document.getElementById('c1'),{{
  type:'bar',
  data:{{labels:tl.map(t=>t.bucket),
         datasets:[
           {{label:'Answered',data:tl.map(t=>t.answered),backgroundColor:'#3b82f6aa',borderColor:'#3b82f6',borderWidth:1}},
           {{label:'Total',   data:tl.map(t=>t.total),   backgroundColor:'#6b728033',borderColor:'#6b7280',borderWidth:1}}
         ]}},
  options:{{responsive:true,plugins:{{legend:{{labels:{{color:'#94a3b8'}}}}}},
            scales:{{x:{{ticks:{{color:'#64748b'}}}},y:{{ticks:{{color:'#64748b'}}}}}}}}
}});
new Chart(document.getElementById('c2'),{{
  type:'doughnut',
  data:{{labels:['Answered','Missed'],
         datasets:[{{data:[{answered},{missed}],backgroundColor:['#3b82f6','#ef4444'],
                     borderColor:'#0f172a',borderWidth:3}}]}},
  options:{{responsive:true,plugins:{{legend:{{labels:{{color:'#94a3b8'}}}}}}}}
}});
</script>
</body>
</html>"""


def fmtdur(s):
    if not s: return "0:00:00"
    return f"{s//3600}:{(s%3600)//60:02d}:{s%60:02d}"


def generate(out_path: str = "/tmp/pw_report.html") -> str:
    s = db.stats()
    summary = s["summary"]
    agents  = s["agents"]
    recent  = s["recent"]
    tl      = s["timeline"]

    answered  = summary.get("answered") or 0
    missed    = summary.get("missed") or 0
    total     = answered + missed
    avg_dur   = summary.get("avg_dur") or 0
    total_dur = summary.get("total_dur") or 0
    asr       = round(answered / total * 100) if total else 0

    agent_rows = "".join(
        f"<tr><td>{i+1}</td><td>{a['name'] or '—'}</td><td><code>{a['extension']}</code></td>"
        f"<td><b>{a['answered']}</b></td><td>{a['missed']}</td>"
        f"<td>{round(a['total_dur']/a['answered'])}s" if a['answered'] else f"<td>—"
        f"</td><td>{fmtdur(a['total_dur'])}</td></tr>"
        for i, a in enumerate(agents[:50])
    )

    call_rows = "".join(
        f"<tr><td>{r['time'] if 'time' in r else '—'}</td>"
        f"<td>{r.get('agent_name') or r.get('agent_ext','')}</td>"
        f"<td><code>{r.get('agent_ext','')}</code></td>"
        f"<td><span class='badge {'badge-ok' if r['outcome']=='answered' else 'badge-miss'}'>{r['outcome']}</span></td>"
        f"<td>{r.get('duration') or 0}s</td></tr>"
        for r in recent[:200]
    )

    html = _TEMPLATE.format(
        ts=time.strftime("%Y-%m-%d %H:%M:%S"),
        answered=answered, missed=missed,
        avg_dur=avg_dur, total_dur=fmtdur(total_dur),
        agents=len([a for a in agents if a["answered"] > 0]),
        asr=asr,
        agent_rows=agent_rows or "<tr><td colspan='7' style='color:#64748b;text-align:center'>No data</td></tr>",
        call_rows=call_rows or "<tr><td colspan='5' style='color:#64748b;text-align:center'>No calls</td></tr>",
        timeline_json=json.dumps(tl),
    )
    Path(out_path).write_text(html)
    return out_path
