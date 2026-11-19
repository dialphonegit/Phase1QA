"""Orchestrate all agent sessions with ramp control and concurrency."""
import asyncio
import logging
from typing import Callable, Awaitable
from playwright.async_api import async_playwright, Browser
from config import HEADLESS, MAX_WORKERS, NUM_AGENTS, HOLD_SECONDS
from agent_session import AgentSession
from ramp_controller import RampController
import db

log = logging.getLogger(__name__)

EventFn = Callable[[dict], Awaitable[None]]


class AgentRunner:
    def __init__(self, creds: list, hold: int = HOLD_SECONDS,
                 max_workers: int = MAX_WORKERS, broadcast: EventFn = None,
                 ramp_profile: str = "instant", ramp_duration: int = 60):
        self.creds        = creds[:NUM_AGENTS]
        self.hold         = hold
        self.max_workers  = max_workers
        self.broadcast    = broadcast
        self.ramp_profile = ramp_profile
        self.ramp_duration = ramp_duration
        self.sessions:    dict[str, AgentSession] = {}
        self._pw          = None
        self._browser: Browser = None
        self._running     = False
        self._tasks:      list[asyncio.Task] = []

    async def start(self):
        db.init()
        for c in self.creds:
            db.upsert_agent(c["extension"], c["email"], c.get("name", c["email"]))

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--use-fake-ui-for-media-stream",
                  "--use-fake-device-for-media-stream",
                  "--disable-web-security"],
        )
        self._running = True
        log.info("Browser up | headless=%s | %d agents | ramp=%s",
                 HEADLESS, len(self.creds), self.ramp_profile)

        sem = asyncio.Semaphore(self.max_workers)
        ramp = RampController(
            len(self.creds), self.ramp_profile, self.ramp_duration,
            broadcast=self.broadcast
        )

        for cred in self.creds:
            sess = AgentSession(cred, self._browser, self._on_event, self.hold)
            self.sessions[cred["extension"]] = sess

        delays = ramp.delays()
        async def _launch(cred, delay):
            if delay > 0:
                await asyncio.sleep(delay)
            async with sem:
                await self.sessions[cred["extension"]].start()

        self._tasks = [
            asyncio.create_task(_launch(c, d))
            for c, d in zip(self.creds, delays)
        ]
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self):
        self._running = False
        for s in self.sessions.values():
            await s.stop()
        for t in self._tasks:
            t.cancel()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        log.info("Runner stopped.")

    def snapshot(self) -> dict:
        return {ext: s.status for ext, s in self.sessions.items()}

    async def _on_event(self, ext: str, event: str, data: dict):
        STATUS_MAP = {
            "online":     "idle",    "offline": "offline",
            "ringing":    "ringing", "answered": "on_call",
            "call_ended": "idle",    "missed": "idle",
            "error":      "error",   "waiting": "idle",
            "logged_in":  "idle",
        }
        if event in STATUS_MAP:
            db.set_status(ext, STATUS_MAP[event])

        if event == "call_ended":
            db.record_call(ext, data.get("ring_at", 0), data.get("answer_at", 0),
                           data.get("end_at", 0), "answered")
        elif event == "missed":
            db.record_call(ext, data.get("ring_at", 0), None, 0, "missed")

        if self.broadcast:
            try:
                await self.broadcast({"type": "agent_event", "ext": ext,
                                      "event": event, "data": data,
                                      "snapshot": self.snapshot(),
                                      "stats": db.stats()})
            except Exception:
                pass
