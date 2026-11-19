"""FastAPI server v3 — adds health-check, alerts, E2E mode, Prometheus /metrics, screenshots."""
import asyncio
import builtins
import json
import logging
import time
from pathlib import Path
from typing import Set

builtins_sum = builtins.sum

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import credentials as creds_mod
import queue_setup
import report_gen
import health_check as hc
from runner import AgentRunner
from caller_runner import CallerRunner
from session_guard import SessionGuard
from queue_monitor import QueueMonitor
from alerts import AlertEngine, AlertRule, DEFAULT_RULES
from config import (SERVER_HOST, SERVER_PORT, CREDS_FILE, NUM_AGENTS,
                    HOLD_SECONDS, MAX_WORKERS, HEADLESS, QUEUE_ID, QUEUE_EXT)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="PW Agent Suite v3", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_clients:  Set[WebSocket] = set()
_runner:   AgentRunner | None = None
_callers:  CallerRunner | None = None
_guard:    SessionGuard | None = None
_monitor:  QueueMonitor | None = None
_alerts:   AlertEngine  | None = None
_bg_tasks: list[asyncio.Task] = []
_start_ts: float = 0.0


# ── Broadcast ──────────────────────────────────────────────────────────────────

async def broadcast(data: dict):
    dead = set()
    for ws in list(_clients):
        try: await ws.send_json(data)
        except Exception: dead.add(ws)
    _clients.difference_update(dead)


# ── Static / WS ───────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")

@app.websocket("/ws")
async def ws_ep(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    await ws.send_json({
        "type": "init",
        "stats":          db.stats(),
        "running":        _runner  is not None,
        "caller_running": _callers is not None,
        "guard_report":   _guard.report()   if _guard  else {},
        "alert_history":  _alerts.get_history() if _alerts else [],
        "uptime":         round(time.time() - _start_ts) if _start_ts else 0,
    })
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect:
        _clients.discard(ws)


# ── Health check ───────────────────────────────────────────────────────────────

@app.get("/api/health")
async def api_health():
    results = await hc.run_all()
    all_ok  = all(r["ok"] for r in results)
    await broadcast({"type": "health_results", "results": results, "ok": all_ok})
    return {"ok": all_ok, "results": results}


# ── Agent runner ───────────────────────────────────────────────────────────────

class StartConfig(BaseModel):
    num_agents:     int   = NUM_AGENTS
    hold_seconds:   int   = HOLD_SECONDS
    max_workers:    int   = MAX_WORKERS
    headless:       bool  = HEADLESS
    creds_file:     str   = CREDS_FILE
    set_forwarding: bool  = True
    enable_guard:   bool  = True
    enable_alerts:  bool  = True
    ramp_profile:   str   = "instant"
    ramp_duration:  int   = 60

@app.post("/api/start")
async def api_start(cfg: StartConfig):
    global _runner, _guard, _alerts, _bg_tasks, _start_ts
    if _runner:
        return JSONResponse({"error": "already running"}, 409)

    cred_list = creds_mod.load(cfg.creds_file)
    if not cred_list:
        return JSONResponse({"error": f"No creds at {cfg.creds_file}"}, 422)
    if cfg.set_forwarding:
        await queue_setup.ensure_forwarding(cred_list[0]["token"])

    import config
    config.HEADLESS    = cfg.headless
    config.MAX_WORKERS = cfg.max_workers
    config.NUM_AGENTS  = cfg.num_agents

    _runner = AgentRunner(cred_list, cfg.hold_seconds, cfg.max_workers, broadcast,
                          cfg.ramp_profile, cfg.ramp_duration)
    _bg_tasks.append(asyncio.create_task(_runner.start()))
    _start_ts = time.time()

    if cfg.enable_guard:
        _guard = SessionGuard(_runner, broadcast)
        _bg_tasks.append(asyncio.create_task(_guard.start()))

    if cfg.enable_alerts:
        _alerts = AlertEngine(DEFAULT_RULES, broadcast, _runner)
        _bg_tasks.append(asyncio.create_task(_alerts.start()))

    n = min(cfg.num_agents, len(cred_list))
    await broadcast({"type": "runner_started", "agents": n, "ramp": cfg.ramp_profile})
    return {"ok": True, "agents": n}

@app.post("/api/stop")
async def api_stop():
    global _runner, _guard, _alerts, _start_ts
    for t in _bg_tasks: t.cancel()
    _bg_tasks.clear()
    if _alerts: await _alerts.stop(); _alerts = None
    if _guard:  await _guard.stop();  _guard  = None
    if _runner: await _runner.stop(); _runner = None
    _start_ts = 0.0
    await broadcast({"type": "runner_stopped"})
    return {"ok": True}

@app.get("/api/status")
async def api_status():
    return {"running":  _runner  is not None,
            "caller_running": _callers is not None,
            "snapshot": _runner.snapshot()   if _runner else {},
            "guard":    _guard.report()      if _guard  else {},
            "alerts":   _alerts.get_history()if _alerts else [],
            "uptime":   round(time.time() - _start_ts) if _start_ts else 0}


# ── E2E mode ───────────────────────────────────────────────────────────────────

class E2EConfig(BaseModel):
    num_agents:   int   = 10
    num_callers:  int   = 10
    hold_seconds: int   = 30
    cps:          float = 1.0
    max_workers:  int   = 10
    headless:     bool  = HEADLESS
    creds_file:   str   = CREDS_FILE
    ramp_profile: str   = "linear"
    ramp_duration: int  = 30

@app.post("/api/e2e/start")
async def api_e2e_start(cfg: E2EConfig):
    """Start agents + callers together as a coordinated E2E test."""
    agent_cfg = StartConfig(num_agents=cfg.num_agents, hold_seconds=cfg.hold_seconds,
                            max_workers=cfg.max_workers, headless=cfg.headless,
                            creds_file=cfg.creds_file, ramp_profile=cfg.ramp_profile,
                            ramp_duration=cfg.ramp_duration, enable_guard=True,
                            enable_alerts=True)
    r = await api_start(agent_cfg)
    if isinstance(r, JSONResponse): return r

    # Delay callers slightly so agents can register first
    async def _delayed_callers():
        await asyncio.sleep(15)
        cred_list = creds_mod.load(cfg.creds_file)
        global _callers
        _callers = CallerRunner(cred_list, QUEUE_EXT, cfg.num_callers,
                                cfg.hold_seconds, cfg.cps, broadcast)
        await _callers.start()
        _callers = None
        await broadcast({"type": "callers_done"})

    asyncio.create_task(_delayed_callers())
    await broadcast({"type": "e2e_started", "agents": cfg.num_agents,
                     "callers": cfg.num_callers, "delay": 15})
    return {"ok": True, "agents": cfg.num_agents, "callers": cfg.num_callers}


# ── SIP Callers ────────────────────────────────────────────────────────────────

class CallerConfig(BaseModel):
    num_callers:  int   = 10
    hold_seconds: int   = 30
    cps:          float = 1.0
    queue_ext:    str   = QUEUE_EXT
    creds_file:   str   = CREDS_FILE

@app.post("/api/callers/start")
async def api_callers_start(cfg: CallerConfig):
    global _callers
    if _callers: return JSONResponse({"error": "callers already running"}, 409)
    cred_list = creds_mod.load(cfg.creds_file)
    if not cred_list: return JSONResponse({"error": "No creds"}, 422)
    _callers = CallerRunner(cred_list, cfg.queue_ext, cfg.num_callers,
                            cfg.hold_seconds, cfg.cps, broadcast)
    async def _run():
        global _callers
        await _callers.start()
        _callers = None
        await broadcast({"type": "callers_done"})
    asyncio.create_task(_run())
    await broadcast({"type": "callers_started", "num": cfg.num_callers})
    return {"ok": True}

@app.post("/api/callers/stop")
async def api_callers_stop():
    global _callers
    if _callers: await _callers.stop(); _callers = None
    return {"ok": True}

@app.get("/api/callers/stats")
async def api_caller_stats():
    return _callers.stats.snapshot() if _callers else {}


# ── Queue monitor ──────────────────────────────────────────────────────────────

@app.post("/api/monitor/start")
async def api_mon_start():
    global _monitor
    creds = creds_mod.load(CREDS_FILE)
    if not creds: return JSONResponse({"error": "No creds"}, 422)
    _monitor = QueueMonitor(creds[0]["token"], [QUEUE_ID], broadcast)
    asyncio.create_task(_monitor.start())
    return {"ok": True}

@app.post("/api/monitor/stop")
async def api_mon_stop():
    global _monitor
    if _monitor: await _monitor.stop(); _monitor = None
    return {"ok": True}


# ── Alerts ────────────────────────────────────────────────────────────────────

@app.get("/api/alerts")
async def api_alerts():
    return {"history": _alerts.get_history() if _alerts else []}

@app.post("/api/alerts/rules")
async def api_alerts_rules(body: dict):
    if _alerts: _alerts.update_rules(body.get("rules", []))
    return {"ok": True}


# ── Selector probe ─────────────────────────────────────────────────────────────

@app.post("/api/probe")
async def api_probe(body: dict = None):
    email = (body or {}).get("email", "shreyanshdubey1122+2@gmail.com")
    from selector_probe import probe
    result = await probe(email)
    return {"ok": True, "selectors": result}


# ── Prometheus /metrics ────────────────────────────────────────────────────────

@app.get("/metrics")
async def prometheus_metrics():
    s    = db.stats()
    smry = s.get("summary", {}) or {}
    snap = _runner.snapshot() if _runner else {}
    online  = builtins_sum(1 for st in snap.values() if st not in ("offline","error"))
    oncall  = builtins_sum(1 for st in snap.values() if st == "on_call")
    ringing = builtins_sum(1 for st in snap.values() if st == "ringing")
    answered = smry.get("answered") or 0
    missed   = smry.get("missed")   or 0
    avg_dur  = smry.get("avg_dur")  or 0
    total_dur= smry.get("total_dur")or 0
    uptime   = round(time.time() - _start_ts) if _start_ts else 0
    lines = [
        "# HELP pw_agents_online Number of agents with non-offline status",
        "# TYPE pw_agents_online gauge",
        f"pw_agents_online {online}",
        "# HELP pw_agents_oncall Agents currently on a call",
        "# TYPE pw_agents_oncall gauge",
        f"pw_agents_oncall {oncall}",
        "# HELP pw_agents_ringing Agents with ringing calls",
        "# TYPE pw_agents_ringing gauge",
        f"pw_agents_ringing {ringing}",
        "# HELP pw_calls_answered_total Total calls answered",
        "# TYPE pw_calls_answered_total counter",
        f"pw_calls_answered_total {answered}",
        "# HELP pw_calls_missed_total Total calls missed",
        "# TYPE pw_calls_missed_total counter",
        f"pw_calls_missed_total {missed}",
        "# HELP pw_avg_duration_seconds Average call hold duration",
        "# TYPE pw_avg_duration_seconds gauge",
        f"pw_avg_duration_seconds {avg_dur}",
        "# HELP pw_total_duration_seconds Total talk time seconds",
        "# TYPE pw_total_duration_seconds counter",
        f"pw_total_duration_seconds {total_dur}",
        "# HELP pw_runner_uptime_seconds Runner uptime in seconds",
        "# TYPE pw_runner_uptime_seconds gauge",
        f"pw_runner_uptime_seconds {uptime}",
    ]
    return PlainTextResponse("\n".join(lines) + "\n")


# ── Reports & DB ───────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def api_stats(): return db.stats()

@app.get("/api/report")
async def api_report():
    return FileResponse(report_gen.generate(), media_type="text/html",
                        headers={"Content-Disposition": "inline; filename=pw_report.html"})

@app.get("/api/report/download")
async def api_report_dl():
    return FileResponse(report_gen.generate(), media_type="text/html",
                        headers={"Content-Disposition": "attachment; filename=pw_report.html"})

@app.delete("/api/db")
async def api_db_reset():
    db.DB_PATH.unlink(missing_ok=True); db.init()
    return {"ok": True}


# ── Credentials ────────────────────────────────────────────────────────────────

class FetchConfig(BaseModel):
    start: int = 1
    end:   int = 100

@app.post("/api/fetch-creds")
async def api_fetch(cfg: FetchConfig):
    emails = creds_mod.member_emails(cfg.start, cfg.end)
    async def _progress(done, total, email, ok):
        await broadcast({"type": "fetch_progress", "done": done, "total": total,
                         "email": email, "ok": ok,
                         "eta_min": round((total-done)*14/60,1)})
    asyncio.create_task(creds_mod.fetch(emails, out_path=CREDS_FILE, progress_cb=_progress))
    return {"ok": True, "total": len(emails), "eta_min": round(len(emails)*14/60,1)}


if __name__ == "__main__":
    db.init()
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="info")
