# PW Agent Suite — Playwright headed-browser load test for DialPhone QA

Drives **N browser "agents"** (1–200) that log into the DialPhone QA portal, go
online, and answer queue calls — a FastAPI control server with a live dashboard,
ramp control, health checks, alerts, Prometheus metrics, and HTML reports.

Each agent is one Playwright browser **context** inside a single Chromium
process. The agents authenticate with a captured JWT (no repeated logins), so a
run of 200 agents does **not** hit the portal's login rate limit.

---

## 1. Requirements

- Python 3.10+
- A member-credentials source (see §4). The default pool is
  `shreyanshdubey1122+N@gmail.com` (N = 1…200), extensions 1000–1199.
- For a **visible** headed browser: an X display. For a **server / headless**
  box: `Xvfb` (see §3 — plain headless Chromium is unreliable here).

## 2. Setup

```bash
cd qa_load_tests/pw_agent_suite

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium          # add --with-deps on a fresh box
```

> If `pip` is missing on a Debian/Ubuntu host:
> `sudo apt-get install -y python3-pip python3-venv` first.

## 3. Headed vs headless — IMPORTANT

`playwright.chromium.launch(headless=True)` can **hang on launch** in some
environments (broken `headless_shell`). Always run the **full (headed)**
Chromium. Two ways to do that:

**A. Visible headed browser on a real desktop** (watch the windows):

```bash
# Point at the logged-in user's X session.
DISPLAY=:0 XAUTHORITY=/run/user/$(id -u)/gdm/Xauthority HEADLESS=false python server.py
```

**B. Headless server (no monitor) — run the real browser under Xvfb:**

```bash
xvfb-run -a --server-args="-screen 0 1920x1080x24" python server.py
```

Xvfb gives you a virtual display, so the real (headed) Chromium runs invisibly
and reliably. This is the recommended way to run on CI / a remote box.

## 4. Credentials

Agents log in by injecting a JWT into `localStorage["web-token"]` (the key the
portal actually uses). The `credentials.py` fetcher obtains those JWTs via the
login API directly:

- Endpoint: `POST {PORTAL_URL}/api/v1/auth/login`
- Body requires a **`device_type`** field — the suite sends `"W"` (Web).
- Token is read from `data.message.token`.

Extensions/passwords are read from a members file when present
(`/home/crown/Downloads/members-credentials.json`), else derived (`+N → 999+N`).

## 5. Run it — N members from 1 to 200

Start the server (headed, §3), then drive it over HTTP (or use the dashboard at
**http://localhost:8765**).

```bash
# 1) Fetch JWTs for members 1..200  (writes /tmp/queue100_creds.json)
curl -s -X POST http://localhost:8765/api/fetch-creds \
     -H 'Content-Type: application/json' \
     -d '{"start":1,"end":200}'

# 2) Start N agents (headed). num_agents is honoured; cap concurrency with max_workers.
curl -s -X POST http://localhost:8765/api/start \
     -H 'Content-Type: application/json' \
     -d '{"num_agents":200,"max_workers":20,"headless":false,
          "ramp_profile":"linear","ramp_duration":60,
          "hold_seconds":30,"set_forwarding":true}'

# 3) Watch live
#    open http://localhost:8765   (grid, live feed, screenshots, charts)

# 4) Stop
curl -s -X POST http://localhost:8765/api/stop
```

Set any N from 1 to 200 in both calls (`end` for the fetch, `num_agents` for the
start). A few hundred browser contexts are heavy — keep `max_workers` modest
(10–20) so agents ramp in instead of all launching at once.

## 6. Configuration (env vars — see `config.py`)

| Var | Default | Meaning |
|-----|---------|---------|
| `PORTAL_URL` | `https://portal.qa.unifiedrtc.com` | Portal base URL |
| `HEADLESS` | `false` | Keep **false** (run headed; use Xvfb for servers) |
| `NUM_AGENTS` | `100` | Default agent count (overridden per-run by `num_agents`) |
| `MAX_WORKERS` | `10` | Concurrent Playwright contexts |
| `HOLD_SECONDS` | `30` | Hold a call before hangup |
| `SERVER_PORT` | `8765` | Dashboard / API port |
| `CREDS_FILE` | `/tmp/queue100_creds.json` | Cached JWTs |
| `QUEUE_EXT` | `0100` | Queue extension used by callers/E2E |

## 7. HTTP API (selected)

| Method & path | Purpose |
|---|---|
| `POST /api/fetch-creds` `{start,end}` | Fetch JWTs for members start..end |
| `POST /api/start` `{num_agents,max_workers,headless,ramp_profile,…}` | Start agents |
| `POST /api/stop` | Stop agents |
| `GET  /api/status` | Running flag + live agent snapshot |
| `POST /api/callers/start` / `POST /api/e2e/start` | SIP callers / combined E2E |
| `GET  /api/health` · `GET /metrics` · `GET /api/report` | Health, Prometheus, HTML report |

## 8. Known limitation

The browser agents reach the portal UI and go online, but the portal's WebRTC
**softphone may not fully register in a headless/automated browser**, so true
media (audio) does not flow. For *actually answering queue calls at the SIP
level* at scale, use the SIP-level harness (`queue_agents_live.py`) instead;
this suite is best for exercising auth, dashboard, presence, and UI load.
