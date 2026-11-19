# Phase1QA

DialPhone QA load-test tooling.

## PW Agent Suite — Playwright headed-browser load test

Run **N browser agents (1–200)** that log into the DialPhone QA portal, go
online, and answer queue calls, with a live dashboard, ramp control, health
checks, and reports.

➡️ **See [`qa_load_tests/pw_agent_suite/README.md`](qa_load_tests/pw_agent_suite/README.md)** for setup and how to run the headed browser with 1–200 members.

Quick start:

```bash
cd qa_load_tests/pw_agent_suite
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && python -m playwright install chromium
# headless server: run the real (headed) browser under Xvfb
xvfb-run -a --server-args="-screen 0 1920x1080x24" python server.py
# then: POST /api/fetch-creds {start:1,end:200}  and  POST /api/start {num_agents:200,...}
```

## Other scripts

Standalone SIP / chat / queue load-test scripts live at the repo root and under
`qa_load_tests/` (see each file's module docstring).
