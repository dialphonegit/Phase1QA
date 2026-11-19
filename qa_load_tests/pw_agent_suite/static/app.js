'use strict';
/* ═══════════════════════════════════════════════════════════════════════════
   PW Agent Suite v3 — app.js
═══════════════════════════════════════════════════════════════════════════ */

// ── State ─────────────────────────────────────────────────────────────────────
const state = {
  agents:       {},   // ext → {status, name, email, answered, missed, total_dur, calls:[]}
  calls:        [],   // recent call rows
  callerStats:  {},
  alerts:       [],
  health:       [],
  queueData:    {},
  screenshots:  [],   // {ext, label, ts, b64}
  tvMode:       false,
  logLines:     [],
  startTs:      null,
  chartTl:      null,
  chartOut:     null,
  ws:           null,
  wsReady:      false,
  monRunning:   false,
};

// ── WebSocket ─────────────────────────────────────────────────────────────────
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  state.ws = new WebSocket(`${proto}://${location.host}/ws`);

  state.ws.onopen = () => {
    state.wsReady = true;
    addLog('sys', 'WebSocket connected');
  };

  state.ws.onclose = () => {
    state.wsReady = false;
    addLog('sys', 'WebSocket disconnected — reconnecting in 3s…');
    setTimeout(connect, 3000);
  };

  state.ws.onmessage = e => {
    try { handleMsg(JSON.parse(e.data)); } catch {}
  };
}

function handleMsg(msg) {
  const { type } = msg;

  if (type === 'init') {
    const s = msg.stats || {};
    (s.agents || []).forEach(a => {
      state.agents[a.extension] = {
        status: a.status || 'offline', name: a.name, email: a.email,
        answered: a.answered || 0, missed: a.missed || 0,
        total_dur: a.total_dur || 0, calls: [],
      };
    });
    state.calls = s.recent || [];
    state.startTs = msg.uptime ? (Date.now()/1000 - msg.uptime) : null;
    if (msg.alert_history) state.alerts = msg.alert_history;
    if (msg.running) showPill('runner', true);
    if (msg.caller_running) showPill('callers', true);
    renderGrid(); renderKPI(s.summary); renderFeed(); renderAlerts();
    return;
  }

  if (type === 'agent_event') {
    const { ext, event, data, snapshot, stats } = msg;
    if (!state.agents[ext]) state.agents[ext] = {status:'offline',name:ext,email:'',answered:0,missed:0,total_dur:0,calls:[]};
    const ag = state.agents[ext];

    if (snapshot) Object.keys(snapshot).forEach(x => {
      if (state.agents[x]) state.agents[x].status = snapshot[x];
    });

    if (event === 'answered')   { ag.answered++;  ag.status='on_call'; }
    if (event === 'missed')     { ag.missed++;     ag.status='idle'; }
    if (event === 'call_ended') {
      ag.status = 'idle';
      ag.total_dur += (data.duration||0);
      state.calls.unshift({ time: Date.now()/1000, ext, outcome:'answered', dur: data.duration||0 });
      state.calls = state.calls.slice(0, 200);
    }
    if (event === 'screenshot') {
      state.screenshots.unshift({ ext, label: data.label, ts: Date.now()/1000, b64: data.b64 });
      state.screenshots = state.screenshots.slice(0, 60);
      renderScreenshots();
      if (document.getElementById('ad-name')?.dataset?.ext === ext) {
        showScreenshotInModal(data.b64);
      }
    }

    addLog(event, `[${ext}] ${event}${data.message ? ': '+data.message : ''}`);
    if (stats?.summary) renderKPI(stats.summary);
    renderGrid();
    renderFeed();
    updateTVWall();
    return;
  }

  if (type === 'runner_started') {
    showPill('runner', true);
    document.getElementById('btn-start').classList.add('hidden');
    document.getElementById('btn-stop').classList.remove('hidden');
    state.startTs = Date.now()/1000;
    toast(`▶ Runner started — ${msg.agents} agents (${msg.ramp})`, 'green');
    addLog('sys', `runner_started agents=${msg.agents}`);
    return;
  }
  if (type === 'runner_stopped') {
    showPill('runner', false);
    document.getElementById('btn-start').classList.remove('hidden');
    document.getElementById('btn-stop').classList.add('hidden');
    state.startTs = null;
    toast('⏹ Runner stopped', 'red'); addLog('sys', 'runner_stopped');
    return;
  }

  if (type === 'callers_started') {
    showPill('callers', true);
    document.getElementById('btn-caller-start').classList.add('hidden');
    document.getElementById('btn-caller-stop').classList.remove('hidden');
    toast(`📞 ${msg.num} callers firing`, 'purple'); addLog('sys', `callers_started num=${msg.num}`);
    return;
  }
  if (type === 'callers_done') {
    showPill('callers', false);
    document.getElementById('btn-caller-start').classList.remove('hidden');
    document.getElementById('btn-caller-stop').classList.add('hidden');
    toast('Callers finished', 'gray'); addLog('sys', 'callers_done');
    return;
  }

  if (type === 'caller_event') {
    const cs = msg.stats || {};
    updateCallerUI(cs);
    addLog('caller', `[caller] ${msg.event||''}`);
    return;
  }

  if (type === 'queue_monitor') {
    state.queueData = msg.queues || {};
    renderQueueCards();
    document.getElementById('mon-ts').textContent = new Date().toLocaleTimeString();
    return;
  }

  if (type === 'health_results') {
    state.health = msg.results || [];
    renderHealthResults();
    return;
  }

  if (type === 'alert') {
    state.alerts.unshift(msg);
    state.alerts = state.alerts.slice(0, 100);
    renderAlerts();
    showPill('alert', true);
    setTimeout(() => showPill('alert', false), 5000);
    const sev = msg.severity || 'warn';
    toast(`⚠ ${msg.name}: ${msg.message}`, sev === 'critical' ? 'red' : 'amber');
    addLog('alert', `ALERT [${sev}] ${msg.name}`);
    return;
  }

  if (type === 'agent_restarted') {
    toast(`🔄 Agent ${msg.ext} restarted (attempt ${msg.attempt})`, 'blue');
    addLog('guard', `agent_restarted ext=${msg.ext} attempt=${msg.attempt}`);
    return;
  }

  if (type === 'e2e_started') {
    toast(`⚡ E2E — ${msg.agents} agents + ${msg.callers} callers (delay ${msg.delay}s)`, 'purple');
    addLog('e2e', `e2e_started agents=${msg.agents} callers=${msg.callers}`);
    return;
  }

  if (type === 'fetch_progress') {
    const pct = Math.round(msg.done/msg.total*100);
    document.getElementById('fetch-bar').style.width = pct + '%';
    document.getElementById('fetch-txt').textContent =
      `${msg.done}/${msg.total} — ${msg.email} ${msg.ok?'✓':'✗'} — ETA ${msg.eta_min}min`;
    addLog('fetch', `fetch ${msg.done}/${msg.total} ${msg.email}`);
    return;
  }

  if (type === 'ramp_progress') {
    addLog('ramp', `ramp ${msg.launched}/${msg.total} (${msg.profile})`);
    return;
  }

  addLog('ws', JSON.stringify(msg).slice(0,120));
}

// ── Grid ──────────────────────────────────────────────────────────────────────
function renderGrid() {
  const st  = document.getElementById('filter-status').value;
  const q   = document.getElementById('filter-search').value.toLowerCase();
  const all = Object.entries(state.agents);

  const filtered = all.filter(([ext, ag]) => {
    if (st && ag.status !== st) return false;
    if (q && !ext.includes(q) && !(ag.name||'').toLowerCase().includes(q)) return false;
    return true;
  });

  document.getElementById('grid-count').textContent = `${filtered.length}/${all.length}`;

  const grid = document.getElementById('agent-grid');
  if (!filtered.length) {
    grid.innerHTML = '<div class="col-span-full text-center text-gray-500 py-10 text-sm">No agents match</div>';
    return;
  }

  grid.innerHTML = filtered.map(([ext, ag]) => {
    const cls = `card-${ag.status}`;
    const dot = `s-${ag.status}`;
    return `<div onclick="openAgentModal('${ext}')"
      class="border rounded-xl p-2.5 cursor-pointer hover:scale-[1.02] transition-transform ${cls}">
      <div class="flex items-center gap-1.5 mb-1">
        <span class="w-2 h-2 rounded-full flex-shrink-0 ${dot}"></span>
        <span class="text-xs font-bold mono truncate">${ext}</span>
      </div>
      <div class="text-xs text-gray-400 truncate leading-tight mb-1">${ag.name||''}</div>
      <div class="flex gap-1.5 text-xs">
        <span class="text-green-400 font-semibold">${ag.answered}</span>
        <span class="text-gray-600">/</span>
        <span class="text-red-400">${ag.missed}</span>
      </div>
    </div>`;
  }).join('');

  // TV grid too
  renderTVGrid(filtered);
}

function renderTVGrid(filtered) {
  const tvg = document.getElementById('tv-grid');
  if (!tvg) return;
  tvg.innerHTML = filtered.slice(0,100).map(([ext, ag]) => {
    const dot = `s-${ag.status}`;
    return `<div class="rounded-lg p-1 text-center border border-gray-800">
      <div class="w-2 h-2 rounded-full mx-auto mb-0.5 ${dot}"></div>
      <div class="text-xs mono leading-tight">${ext}</div>
    </div>`;
  }).join('');
}

// ── KPI ───────────────────────────────────────────────────────────────────────
function renderKPI(sum) {
  if (!sum) return;
  const snap = state.agents;
  let online=0, oncall=0, ringing=0, offline=0;
  Object.values(snap).forEach(ag => {
    if (ag.status==='on_call')   oncall++;
    else if (ag.status==='ringing') ringing++;
    else if (ag.status==='idle' || ag.status==='waiting') online++;
    else offline++;
  });
  setText('k-online',  online);
  setText('k-oncall',  oncall);
  setText('k-ring',    ringing);
  setText('k-offline', offline);
  setText('k-answered',sum.answered||0);
  setText('k-missed',  sum.missed||0);
  setText('k-avgdur',  sum.avg_dur ? fmtDur(sum.avg_dur) : '—');
  if (state.startTs) {
    const up = Math.round(Date.now()/1000 - state.startTs);
    setText('k-uptime', fmtDur(up));
  }

  // TV wall KPIs
  setText('tv-answered', sum.answered||0);
  setText('tv-missed',   sum.missed||0);
  setText('tv-oncall',   oncall);
  const asr = sum.answered ? Math.round(sum.answered/(sum.answered+(sum.missed||0))*100) : 0;
  setText('tv-asr', asr + '%');
}

// ── Feed ──────────────────────────────────────────────────────────────────────
function renderFeed() {
  const tbody = document.getElementById('feed-tbody');
  document.getElementById('feed-count').textContent = state.calls.length + ' calls';
  if (!state.calls.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-10 text-center text-gray-500">No calls yet</td></tr>';
    return;
  }
  tbody.innerHTML = state.calls.slice(0,100).map(c => {
    const cl = c.outcome==='answered' ? 'text-green-400' : 'text-red-400';
    return `<tr class="border-b border-gray-900 hover:bg-gray-900/50">
      <td class="px-4 py-1.5 mono text-gray-400">${new Date(c.time*1000).toLocaleTimeString()}</td>
      <td class="px-4 py-1.5">${(state.agents[c.ext]?.name||c.ext).slice(0,20)}</td>
      <td class="px-4 py-1.5 mono font-semibold">${c.ext}</td>
      <td class="px-4 py-1.5 text-gray-400">${c.caller||'—'}</td>
      <td class="px-4 py-1.5 ${cl} font-semibold">${c.outcome}</td>
      <td class="px-4 py-1.5 text-right mono">${c.dur ? fmtDur(c.dur) : '—'}</td>
    </tr>`;
  }).join('');

  // TV feed
  const tvFeed = document.getElementById('tv-feed');
  tvFeed.innerHTML = state.calls.slice(0,8).map(c => {
    const cl = c.outcome==='answered' ? 'text-green-400' : 'text-red-400';
    return `<div class="flex justify-between ${cl}">
      <span>${c.ext}</span><span>${c.outcome}</span><span>${c.dur ? fmtDur(c.dur) : ''}</span>
    </div>`;
  }).join('');
}

// ── Caller UI ─────────────────────────────────────────────────────────────────
function updateCallerUI(cs) {
  state.callerStats = cs;
  setText('cs-placed',   cs.placed||0);
  setText('cs-answered', cs.answered||0);
  setText('cs-active',   cs.active||0);
  setText('cs-failed',   cs.failed||0);
  const asr = cs.placed ? Math.round((cs.answered||0)/cs.placed*100) : 0;
  document.getElementById('cs-asr-bar').style.width = asr + '%';
  document.getElementById('cs-asr-txt').textContent = asr + '%';
}

// ── Queue cards ───────────────────────────────────────────────────────────────
function renderQueueCards() {
  const el = document.getElementById('queue-cards');
  const qs = Array.isArray(state.queueData) ? state.queueData : Object.values(state.queueData);
  if (!qs.length) {
    el.innerHTML = '<div class="col-span-full text-center text-gray-500 py-10 text-sm">No queues</div>';
    return;
  }
  el.innerHTML = qs.map(q => `
    <div class="bg-gray-900 rounded-2xl border border-gray-800 p-4">
      <div class="font-semibold mb-2 truncate">${q.name||q.id}</div>
      <div class="grid grid-cols-2 gap-2 text-sm">
        <div class="bg-gray-800 rounded-lg p-2 text-center"><div class="text-xl font-bold text-green-400 mono">${q.free||0}</div><div class="text-xs text-gray-500">Free</div></div>
        <div class="bg-gray-800 rounded-lg p-2 text-center"><div class="text-xl font-bold text-blue-400 mono">${q.busy||0}</div><div class="text-xs text-gray-500">Busy</div></div>
        <div class="bg-gray-800 rounded-lg p-2 text-center"><div class="text-xl font-bold text-amber-400 mono">${q.waiting||0}</div><div class="text-xs text-gray-500">Waiting</div></div>
        <div class="bg-gray-800 rounded-lg p-2 text-center"><div class="text-xl font-bold mono">${q.total||0}</div><div class="text-xs text-gray-500">Total</div></div>
      </div>
    </div>`).join('');
}

// ── Screenshots ───────────────────────────────────────────────────────────────
function renderScreenshots() {
  const el = document.getElementById('screenshot-grid');
  if (!state.screenshots.length) {
    el.innerHTML = '<div class="col-span-full text-center text-gray-500 py-10 text-sm">No screenshots yet</div>';
    return;
  }
  el.innerHTML = state.screenshots.map(s => `
    <div class="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
      <img src="data:image/png;base64,${s.b64}" class="w-full" alt="${s.label}">
      <div class="px-3 py-2 text-xs mono text-gray-400 flex justify-between">
        <span>${s.ext}</span><span class="text-gray-500">${s.label}</span>
      </div>
    </div>`).join('');
}

function showScreenshotInModal(b64) {
  const wrap = document.getElementById('ad-screenshot');
  const img  = document.getElementById('ad-img');
  img.src = 'data:image/png;base64,' + b64;
  wrap.classList.remove('hidden');
}

// ── Alerts ────────────────────────────────────────────────────────────────────
function renderAlerts() {
  const el = document.getElementById('alert-history');
  document.getElementById('alert-count').textContent = `(${state.alerts.length})`;
  if (!state.alerts.length) {
    el.innerHTML = '<div class="text-gray-500 text-xs py-4">No alerts fired yet.</div>';
    return;
  }
  el.innerHTML = state.alerts.slice(0,50).map(a => {
    const cls = a.severity==='critical' ? 'alert-critical' : 'alert-warn';
    const ts  = a.ts ? new Date(a.ts*1000).toLocaleTimeString() : '';
    return `<div class="toast-item ${cls}">
      <div class="font-semibold">${a.name} <span class="text-xs opacity-70 font-normal ml-2">${ts}</span></div>
      <div class="opacity-80">${a.message||''}</div>
    </div>`;
  }).join('');

  // Render rules (fetch from server)
  fetchAlertRules();
}

function fetchAlertRules() {
  fetch('/api/status').then(r=>r.json()).then(data => {
    // rules come from status or alerts endpoint
  }).catch(()=>{});
}

// ── Health ────────────────────────────────────────────────────────────────────
async function runHealthCheck() {
  const spin = document.getElementById('health-spin');
  spin.classList.remove('hidden');
  openTab('health');
  try {
    const r = await fetch('/api/health');
    const data = await r.json();
    state.health = data.results || [];
    renderHealthResults();
  } catch(e) {
    toast('Health check failed: '+e.message, 'red');
  } finally {
    spin.classList.add('hidden');
  }
}

function renderHealthResults() {
  const el = document.getElementById('health-results');
  if (!state.health.length) {
    el.innerHTML = '<div class="text-gray-500 text-sm">No results yet.</div>';
    return;
  }
  el.innerHTML = state.health.map(r => {
    const icon = r.ok ? '✅' : '❌';
    const cls  = r.ok ? 'probe-ok' : 'probe-fail';
    return `<div class="flex items-start gap-3 p-3 bg-gray-900 rounded-xl border border-gray-800">
      <span class="text-base mt-0.5">${icon}</span>
      <div class="flex-1">
        <div class="font-semibold text-sm ${cls}">${r.name}</div>
        <div class="text-xs text-gray-400 mono mt-0.5">${r.msg||''}</div>
      </div>
      <div class="text-xs mono text-gray-500 mt-0.5">${r.ms ? r.ms+'ms' : ''}</div>
    </div>`;
  }).join('');
}

async function runProbe() {
  const email = document.getElementById('probe-email').value.trim();
  const el = document.getElementById('probe-results');
  el.innerHTML = '<div class="text-gray-400">Running headless probe…</div>';
  try {
    const r = await fetch('/api/probe', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({email}),
    });
    const data = await r.json();
    const sels = data.selectors || {};
    el.innerHTML = Object.entries(sels).map(([k,v]) => {
      const cls = v ? 'probe-ok' : 'probe-fail';
      return `<div class="${cls}"><span class="text-gray-500">${k.padEnd(14,' ')}</span> ${v||'NOT FOUND'}</div>`;
    }).join('');
    toast('Probe complete — check results', 'green');
  } catch(e) {
    el.innerHTML = `<div class="probe-fail">Error: ${e.message}</div>`;
    toast('Probe failed: '+e.message, 'red');
  }
}

// ── Charts ────────────────────────────────────────────────────────────────────
function initCharts() {
  const dark = { color: '#9ca3af' };
  const grid = { color: '#1f2937' };

  state.chartTl = new Chart(document.getElementById('chart-tl'), {
    type: 'bar',
    data: { labels: [], datasets: [
      { label: 'Answered', data: [], backgroundColor: '#22c55e88' },
      { label: 'Missed',   data: [], backgroundColor: '#ef444488' },
    ]},
    options: { responsive: true, plugins: { legend: { labels: dark }},
      scales: { x: { ticks: dark, grid }, y: { ticks: dark, grid }}}
  });

  state.chartOut = new Chart(document.getElementById('chart-out'), {
    type: 'doughnut',
    data: { labels: ['Answered','Missed'], datasets: [{
      data: [0,0], backgroundColor: ['#22c55e','#ef4444'], borderWidth: 0,
    }]},
    options: { responsive: true, plugins: { legend: { labels: dark }}}
  });
}

function refreshCharts() {
  fetch('/api/stats').then(r=>r.json()).then(data => {
    const tl = data.timeline || [];
    if (state.chartTl) {
      state.chartTl.data.labels = tl.map(x=>x.hour);
      state.chartTl.data.datasets[0].data = tl.map(x=>x.answered);
      state.chartTl.data.datasets[1].data = tl.map(x=>x.missed);
      state.chartTl.update();
    }
    const sum = data.summary || {};
    if (state.chartOut) {
      state.chartOut.data.datasets[0].data = [sum.answered||0, sum.missed||0];
      state.chartOut.update();
    }
    renderLeaderboard(data.agents || []);
    renderKPI(sum);
  }).catch(()=>{});
}

function renderLeaderboard(agents) {
  const tbody = document.getElementById('report-tbody');
  const sorted = agents.slice().sort((a,b) => (b.answered||0)-(a.answered||0));
  tbody.innerHTML = sorted.slice(0,20).map((a,i) => {
    const avg = a.answered ? Math.round((a.total_dur||0)/a.answered) : 0;
    return `<tr class="hover:bg-gray-900/40">
      <td class="px-4 py-1.5 text-gray-500">${i+1}</td>
      <td class="px-4 py-1.5 truncate max-w-[140px]">${a.name||a.email}</td>
      <td class="px-4 py-1.5 mono font-semibold">${a.extension}</td>
      <td class="px-4 py-1.5 text-right text-green-400 font-bold">${a.answered||0}</td>
      <td class="px-4 py-1.5 text-right text-red-400">${a.missed||0}</td>
      <td class="px-4 py-1.5 text-right mono">${avg ? fmtDur(avg) : '—'}</td>
      <td class="px-4 py-1.5 text-right mono">${a.total_dur ? fmtDur(a.total_dur) : '—'}</td>
    </tr>`;
  }).join('');
}

// ── Log ───────────────────────────────────────────────────────────────────────
const LOG_COLORS = {
  sys:    'text-blue-400', alert: 'text-red-400', error:  'text-red-300',
  guard:  'text-cyan-400', ramp:  'text-purple-400', caller: 'text-purple-300',
  fetch:  'text-yellow-400', e2e: 'text-green-400',
};
function addLog(type, msg) {
  const ts = new Date().toLocaleTimeString();
  state.logLines.unshift({ ts, type, msg });
  state.logLines = state.logLines.slice(0, 500);
  const el = document.getElementById('event-log');
  if (!el) return;
  const cl = LOG_COLORS[type] || 'text-gray-300';
  const row = document.createElement('div');
  row.className = `${cl} py-px leading-tight`;
  row.textContent = `${ts}  [${type.padEnd(8)}]  ${msg}`;
  el.insertBefore(row, el.firstChild);
  while (el.children.length > 500) el.removeChild(el.lastChild);
  document.getElementById('log-count').textContent = state.logLines.length;
}
function clearLog() {
  state.logLines = [];
  document.getElementById('event-log').innerHTML = '';
  document.getElementById('log-count').textContent = '0';
}

// ── Runner controls ───────────────────────────────────────────────────────────
async function startRunner() {
  const cfg = readSettings();
  try {
    const r = await fetch('/api/start', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(cfg),
    });
    const data = await r.json();
    if (data.error) toast('Start failed: '+data.error, 'red');
  } catch(e) { toast('Error: '+e.message, 'red'); }
}

async function stopAll() {
  await fetch('/api/stop', {method:'POST'});
  await fetch('/api/callers/stop', {method:'POST'});
  toast('Stop requested', 'red');
}

async function startCallers() {
  const cfg = {
    num_callers:  +document.getElementById('cc-num').value,
    hold_seconds: +document.getElementById('cc-hold').value,
    cps:          +document.getElementById('cc-cps').value,
    queue_ext:     document.getElementById('cc-queue').value,
    creds_file:    document.getElementById('cfg-creds')?.value || '/tmp/queue100_creds.json',
  };
  const r = await fetch('/api/callers/start', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(cfg),
  });
  const data = await r.json();
  if (data.error) toast('Callers: '+data.error, 'red');
}

async function stopCallers() {
  await fetch('/api/callers/stop', {method:'POST'});
}

async function startMonitor() {
  document.getElementById('btn-mon-on').classList.add('hidden');
  document.getElementById('btn-mon-off').classList.remove('hidden');
  state.monRunning = true;
  const r = await fetch('/api/monitor/start', {method:'POST'});
  const data = await r.json();
  if (data.error) toast('Monitor: '+data.error, 'red');
}
async function stopMonitor() {
  document.getElementById('btn-mon-on').classList.remove('hidden');
  document.getElementById('btn-mon-off').classList.add('hidden');
  state.monRunning = false;
  await fetch('/api/monitor/stop', {method:'POST'});
}

async function startE2E() {
  const cfg = readSettings();
  const e2e = {
    num_agents:   cfg.num_agents,
    num_callers:  +document.getElementById('cc-num').value || 10,
    hold_seconds: cfg.hold_seconds,
    cps:          +document.getElementById('cc-cps').value || 1,
    max_workers:  cfg.max_workers,
    headless:     cfg.headless,
    creds_file:   cfg.creds_file,
    ramp_profile: cfg.ramp_profile,
    ramp_duration:cfg.ramp_duration,
  };
  const r = await fetch('/api/e2e/start', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(e2e),
  });
  const data = await r.json();
  if (data.error) toast('E2E: '+data.error, 'red');
}

async function startFetch() {
  const start = +document.getElementById('cfg-fs').value;
  const end   = +document.getElementById('cfg-fe').value;
  document.getElementById('fetch-prog').classList.remove('hidden');
  const r = await fetch('/api/fetch-creds', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({start, end}),
  });
  const data = await r.json();
  if (data.error) { toast('Fetch: '+data.error,'red'); return; }
  toast(`Fetching ${data.total} creds (~${data.eta_min}min)…`, 'purple');
}

async function resetDB() {
  if (!confirm('Reset all call records?')) return;
  await fetch('/api/db', {method:'DELETE'});
  state.calls = [];
  state.agents = {};
  renderGrid(); renderFeed();
  toast('DB reset', 'gray');
}

function exportCSV() {
  const rows = [['Time','Ext','Outcome','Duration']];
  state.calls.forEach(c => rows.push([
    new Date(c.time*1000).toISOString(), c.ext, c.outcome, c.dur||0
  ]));
  const csv = rows.map(r=>r.join(',')).join('\n');
  dlFile('pw_calls.csv', csv, 'text/csv');
}

function dlFile(name, content, type) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([content], {type}));
  a.download = name; a.click();
}

// ── Settings ──────────────────────────────────────────────────────────────────
function readSettings() {
  return {
    num_agents:    +document.getElementById('cfg-agents').value,
    hold_seconds:  +document.getElementById('cfg-hold').value,
    max_workers:   +document.getElementById('cfg-workers').value,
    ramp_profile:   document.getElementById('cfg-ramp').value,
    ramp_duration: +document.getElementById('cfg-ramp-dur').value,
    headless:       document.getElementById('cfg-headless').checked,
    set_forwarding: document.getElementById('cfg-setfwd').checked,
    enable_guard:   document.getElementById('cfg-guard').checked,
    enable_alerts:  document.getElementById('cfg-alerts-en').checked,
    creds_file:     document.getElementById('cfg-creds').value,
  };
}

function openSettings() {
  document.getElementById('modal-settings').classList.remove('hidden');
}
function closeSettings() {
  document.getElementById('modal-settings').classList.add('hidden');
}
function saveAndStart() {
  closeSettings();
  startRunner();
}

// ── Agent detail modal ────────────────────────────────────────────────────────
function openAgentModal(ext) {
  const ag = state.agents[ext];
  if (!ag) return;
  document.getElementById('ad-name').textContent = ag.name || ext;
  document.getElementById('ad-name').dataset.ext = ext;
  document.getElementById('ad-email').textContent = ag.email || '';
  document.getElementById('ad-answered').textContent = ag.answered;
  document.getElementById('ad-missed').textContent   = ag.missed;
  document.getElementById('ad-dur').textContent = ag.total_dur ? fmtDur(ag.total_dur) : '—';
  document.getElementById('ad-status').textContent   = ag.status;

  // Show latest screenshot if available
  const shot = state.screenshots.find(s=>s.ext===ext);
  if (shot) showScreenshotInModal(shot.b64);
  else document.getElementById('ad-screenshot').classList.add('hidden');

  document.getElementById('modal-agent').classList.remove('hidden');
}
function closeAgentModal() {
  document.getElementById('modal-agent').classList.add('hidden');
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
function switchTab(name, btn) {
  document.querySelectorAll('[id^="tab-"]').forEach(el=>el.classList.add('hidden'));
  document.querySelectorAll('.tab-btn').forEach(el=>el.classList.remove('active'));
  document.getElementById('tab-'+name)?.classList.remove('hidden');
  if (btn) btn.classList.add('active');
  if (name==='reports') refreshCharts();
  if (name==='alerts')  refreshAlertRulesUI();
}

function openTab(name) {
  const btn = document.querySelector(`[data-tab="${name}"]`);
  switchTab(name, btn);
}

function refreshAlertRulesUI() {
  fetch('/api/status').then(r=>r.json()).then(d=>{
    const rules = d.alerts_rules || [];
    const el = document.getElementById('alert-rules');
    if (!rules.length) {
      el.innerHTML = '<div class="text-gray-500">Default rules active (edit server alerts.py)</div>';
      return;
    }
    el.innerHTML = rules.map(r => `
      <div class="p-3 bg-gray-900 rounded-xl border border-gray-800 flex items-center justify-between gap-3">
        <div><div class="font-semibold text-sm">${r.name}</div>
          <div class="text-xs text-gray-400">${r.metric} ${r.op} ${r.threshold}</div></div>
        <span class="px-2 py-0.5 rounded-full text-xs font-semibold ${r.severity==='critical'?'alert-critical':'alert-warn'}">${r.severity}</span>
      </div>`).join('');
  });
}

// ── TV Wall ───────────────────────────────────────────────────────────────────
function toggleTV() {
  state.tvMode = !state.tvMode;
  document.body.classList.toggle('tv-mode', state.tvMode);
  if (state.tvMode) {
    updateTVWall();
    toast('📺 TV Wall mode — press Esc to exit', 'blue');
  }
}

function updateTVWall() {
  if (!state.tvMode) return;
  const all = Object.entries(state.agents);
  renderTVGrid(all);
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && state.tvMode) toggleTV();
  if (e.key === 'Escape') {
    closeSettings();
    closeAgentModal();
  }
});

// ── Helpers ───────────────────────────────────────────────────────────────────
function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function fmtDur(sec) {
  if (!sec) return '0s';
  const h = Math.floor(sec/3600);
  const m = Math.floor((sec%3600)/60);
  const s = sec%60;
  if (h) return `${h}h${m}m`;
  if (m) return `${m}m${s}s`;
  return `${s}s`;
}

function showPill(name, show) {
  const el = document.getElementById(`pill-${name}`);
  if (!el) return;
  el.classList.toggle('hidden', !show);
  el.classList.toggle('flex', show);
}

const TOAST_CLS = {
  green:  'bg-green-900/60 border-green-800 text-green-300',
  red:    'bg-red-900/60 border-red-800 text-red-300',
  purple: 'bg-purple-900/60 border-purple-800 text-purple-300',
  amber:  'bg-amber-900/60 border-amber-800 text-amber-300',
  blue:   'bg-blue-900/60 border-blue-800 text-blue-300',
  gray:   'bg-gray-800 border-gray-700 text-gray-300',
};
function toast(msg, color='gray', dur=4000) {
  const wrap = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = `toast-item ${TOAST_CLS[color]||TOAST_CLS.gray}`;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), dur);
}

// ── Periodic refresh ──────────────────────────────────────────────────────────
setInterval(() => {
  if (!state.wsReady) return;
  if (document.getElementById('tab-reports')?.classList.contains('hidden') === false) {
    refreshCharts();
  }
  if (state.startTs) {
    const up = Math.round(Date.now()/1000 - state.startTs);
    setText('k-uptime', fmtDur(up));
  }
}, 5000);

// Auto-refresh caller stats when callers tab visible
setInterval(() => {
  const tab = document.getElementById('tab-callers');
  if (!tab || tab.classList.contains('hidden')) return;
  fetch('/api/callers/stats').then(r=>r.json()).then(cs => {
    if (cs && cs.placed !== undefined) updateCallerUI(cs);
  }).catch(()=>{});
}, 3000);

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initCharts();
  connect();
  // Show first tab
  switchTab('agents', document.querySelector('[data-tab="agents"]'));
});
