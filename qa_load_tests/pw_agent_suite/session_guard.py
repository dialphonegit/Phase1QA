"""Watchdog: monitors agent sessions and restarts dead ones automatically."""
import asyncio
import logging
import time

log = logging.getLogger(__name__)

CHECK_INTERVAL = 20   # seconds
MAX_IDLE_SECS  = 120  # restart if offline/error for longer than this


class SessionGuard:
    def __init__(self, runner, broadcast=None):
        self.runner    = runner
        self.broadcast = broadcast
        self._running  = False
        self._restarts: dict[str, int]   = {}
        self._last_ok:  dict[str, float] = {}

    async def start(self):
        self._running = True
        log.info("SessionGuard active (check every %ds)", CHECK_INTERVAL)
        while self._running:
            await asyncio.sleep(CHECK_INTERVAL)
            await self._check()

    async def stop(self):
        self._running = False

    async def _check(self):
        now = time.time()
        restarted = 0
        for ext, sess in list(self.runner.sessions.items()):
            if sess.status not in ("offline", "error"):
                self._last_ok[ext] = now
            idle = now - self._last_ok.get(ext, 0)
            if sess.status in ("offline", "error") and idle > MAX_IDLE_SECS:
                await self._restart(ext, sess)
                restarted += 1
        if restarted and self.broadcast:
            await self.broadcast({"type": "guard_report",
                                  "restarted": restarted,
                                  "total_restarts": sum(self._restarts.values())})

    async def _restart(self, ext: str, sess):
        count = self._restarts.get(ext, 0) + 1
        if count > 5:
            log.warning("[guard] %s hit restart limit", ext)
            return
        self._restarts[ext] = count
        log.info("[guard] restarting %s (attempt %d)", ext, count)
        await sess.stop()
        await asyncio.sleep(2 * count)   # back-off
        from agent_session import AgentSession
        new = AgentSession(sess.cred, self.runner._browser,
                           self.runner._on_event, self.runner.hold)
        self.runner.sessions[ext] = new
        self._last_ok[ext] = time.time()
        asyncio.create_task(new.start())
        if self.broadcast:
            await self.broadcast({"type": "agent_restarted", "ext": ext, "attempt": count})

    def report(self) -> dict:
        return {"total": sum(self._restarts.values()), "per_agent": dict(self._restarts)}
