"""Poll the portal API every N seconds and push live queue stats to the dashboard."""
import asyncio
import logging
import time
import aiohttp
from config import PORTAL_URL

log = logging.getLogger(__name__)
POLL_INTERVAL = 8  # seconds


class QueueMonitor:
    def __init__(self, token: str, queue_ids: list[str], broadcast=None):
        self.token     = token
        self.queue_ids = queue_ids
        self.broadcast = broadcast
        self._running  = False
        self._cache: dict = {}

    async def start(self):
        self._running = True
        log.info("QueueMonitor polling every %ds", POLL_INTERVAL)
        while self._running:
            try:
                data = await self._fetch()
                if data and self.broadcast:
                    await self.broadcast({"type": "queue_monitor", "ts": time.time(), "queues": data})
                self._cache = data or self._cache
            except Exception as e:
                log.warning("QueueMonitor poll error: %s", e)
            await asyncio.sleep(POLL_INTERVAL)

    async def stop(self):
        self._running = False

    async def _fetch(self) -> list[dict]:
        hdrs = {"Authorization": f"Bearer {self.token}"}
        async with aiohttp.ClientSession() as s:
            # Queue list
            async with s.post(f"{PORTAL_URL}/api/v1/queue/list",
                              json={"page": 1, "limit": 50},
                              headers=hdrs, ssl=False) as r:
                body = await r.json(content_type=None)
            rows = body.get("data", {}).get("result", {}).get("rows", [])

            # Filter to relevant queues
            if self.queue_ids:
                rows = [q for q in rows if q["_id"] in self.queue_ids]

            # Agent monitor overview
            try:
                async with s.get(f"{PORTAL_URL}/api/v1/agent-monitor/overview",
                                 headers=hdrs, ssl=False) as r2:
                    overview = await r2.json(content_type=None)
            except Exception:
                overview = {}

            return [{
                "_id":       q["_id"],
                "name":      q["name"],
                "extension": q.get("extension"),
                "members":   len(q.get("members", [])),
                "routing":   q.get("routing_algorithm", "linear"),
            } for q in rows]

    def latest(self) -> dict:
        return self._cache
