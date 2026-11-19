"""Verify/set DID call forwarding to the 100-agent queue via REST API."""
import aiohttp
import logging
from config import PORTAL_URL, DID_UUID, QUEUE_ID, QUEUE_NAME, DID_NUMBER

log = logging.getLogger(__name__)

_FWD_PAYLOAD = {
    "uuid": DID_UUID,
    "forward_call_actions": {
        "call_handling": {
            "business_hours": {
                "name":  QUEUE_NAME,
                "value": QUEUE_ID,
                "type":  "queue",
            }
        },
        "media": {
            "welcome":   {"enabled": False, "label": "", "value": ""},
            "hold":      {"enabled": False, "label": "", "value": ""},
            "voicemail": {"enabled": False, "label": "", "value": ""},
        },
        "did_info": {"did_name": f"Vonage {DID_NUMBER.replace(' ','')}"},
    },
}


async def get_forwarding(token: str) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{PORTAL_URL}/api/v1/did/number/get-forwarding-info",
            json={"uuid": DID_UUID},
            headers={"Authorization": f"Bearer {token}"},
            ssl=False,
        ) as r:
            return await r.json(content_type=None)


async def set_forwarding(token: str) -> bool:
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{PORTAL_URL}/api/v1/did/number/set-forwarding",
            json=_FWD_PAYLOAD,
            headers={"Authorization": f"Bearer {token}"},
            ssl=False,
        ) as r:
            body = await r.json(content_type=None)
            ok = body.get("success", False)
            if ok:
                log.info("DID %s → %s (%s)", DID_NUMBER, QUEUE_NAME, QUEUE_ID)
            else:
                log.error("set-forwarding failed: %s", body)
            return ok


async def ensure_forwarding(token: str) -> bool:
    """Set forwarding only if not already pointing at our queue."""
    info = await get_forwarding(token)
    bh = info.get("data", {}).get("forward_call_actions", {}) \
                              .get("call_handling", {}) \
                              .get("business_hours", {})
    if bh.get("value") == QUEUE_ID:
        log.info("DID already forwarding to %s — skipping", QUEUE_NAME)
        return True
    return await set_forwarding(token)
