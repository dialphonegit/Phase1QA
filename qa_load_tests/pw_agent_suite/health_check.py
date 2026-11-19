"""Pre-flight validation — run before starting agents to catch config issues early."""
import asyncio
import time
import aiohttp
from config import PORTAL_URL, QUEUE_ID, DID_UUID, CREDS_FILE
import credentials as creds_mod

CHECKS = []


def check(name):
    def _d(fn):
        CHECKS.append((name, fn))
        return fn
    return _d


@check("Credentials file readable")
async def _creds(token, session):
    creds = creds_mod.load(CREDS_FILE)
    if not creds:
        return False, f"No creds at {CREDS_FILE} — run Fetch Creds first"
    age = time.time() - creds_mod.load.__module__ and 0  # always fresh on load
    return True, f"{len(creds)} accounts loaded"


@check("Portal reachable")
async def _portal(token, session):
    try:
        async with session.get(PORTAL_URL, ssl=False, timeout=aiohttp.ClientTimeout(total=8)) as r:
            return r.status < 500, f"HTTP {r.status}"
    except Exception as e:
        return False, str(e)


@check("JWT token valid")
async def _token(token, session):
    if not token:
        return False, "No token — load creds first"
    async with session.get(f"{PORTAL_URL}/api/v1/user/me",
                           headers={"Authorization": f"Bearer {token}"},
                           ssl=False, timeout=aiohttp.ClientTimeout(total=8)) as r:
        body = await r.json(content_type=None)
        ok = body.get("success", False)
        name = body.get("data", {}).get("name", "")
        return ok, f"Logged in as {name}" if ok else body.get("error", {}).get("message", "Invalid")


@check("Queue exists")
async def _queue(token, session):
    async with session.post(f"{PORTAL_URL}/api/v1/queue/list",
                            json={"page": 1, "limit": 50},
                            headers={"Authorization": f"Bearer {token}"},
                            ssl=False) as r:
        body = await r.json(content_type=None)
    rows = body.get("data", {}).get("result", {}).get("rows", [])
    match = next((q for q in rows if q["_id"] == QUEUE_ID), None)
    if match:
        return True, f"{match['name']} (ext {match.get('extension')}, {len(match.get('members',[]))} members)"
    return False, f"Queue ID {QUEUE_ID} not found"


@check("DID forwarding set")
async def _did(token, session):
    async with session.post(f"{PORTAL_URL}/api/v1/did/number/get-forwarding-info",
                            json={"uuid": DID_UUID},
                            headers={"Authorization": f"Bearer {token}"},
                            ssl=False) as r:
        body = await r.json(content_type=None)
    bh = (body.get("data", {}).get("forward_call_actions", {})
               .get("call_handling", {}).get("business_hours", {}))
    if bh.get("value") == QUEUE_ID:
        return True, f"DID → {bh.get('name')} ✓"
    return False, f"DID points to '{bh.get('name', 'unknown')}' not target queue"


@check("WebSocket endpoint reachable")
async def _ws(token, session):
    import websockets, ssl as ssl_mod
    ctx = ssl_mod.create_default_context()
    ctx.check_hostname = False; ctx.verify_mode = ssl_mod.CERT_NONE
    try:
        async with websockets.connect("wss://app.qa.unifiedrtc.com/ws/",
                                      subprotocols=["sip"], ssl=ctx,
                                      open_timeout=8) as ws:
            return True, "WSS reachable"
    except Exception as e:
        return False, str(e)


async def run_all(token: str = None) -> list[dict]:
    creds = creds_mod.load(CREDS_FILE)
    if not token and creds:
        token = creds[0]["token"]

    results = []
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        for name, fn in CHECKS:
            t0 = time.time()
            try:
                ok, msg = await fn(token, session)
            except Exception as e:
                ok, msg = False, str(e)
            results.append({"name": name, "ok": ok, "msg": msg,
                            "ms": round((time.time() - t0) * 1000)})
    return results
