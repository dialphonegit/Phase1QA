"""Orchestrate all agent sessions with ramp control and concurrency."""
from __future__ import annotations
import asyncio
import logging
import math
from typing import Callable, Awaitable
from playwright.async_api import async_playwright, Browser
from config import MAX_WORKERS, NUM_AGENTS, HOLD_SECONDS, CONTEXTS_PER_BROWSER
from agent_session import AgentSession
from ramp_controller import RampController
import config
import db

log = logging.getLogger(__name__)

EventFn = Callable[[dict], Awaitable[None]]


class AgentRunner:
    def __init__(self, creds: list, hold: int = HOLD_SECONDS,
                 max_workers: int = MAX_WORKERS, broadcast: EventFn = None,
                 ramp_profile: str = "instant", ramp_duration: int = 60,
                 num_agents: int | None = None, headed_count: int = 0):
        # Honour the requested agent count (falls back to config NUM_AGENTS).
        n = num_agents if num_agents is not None else NUM_AGENTS
        self.creds        = creds[:n]
        self.hold         = hold
        self.max_workers  = max_workers
        self.broadcast    = broadcast
        self.ramp_profile = ramp_profile
        self.ramp_duration = ramp_duration
        # Run the first `headed_count` agents in a VISIBLE (headed) browser and
        # the rest headless. 0 = all agents follow the global HEADLESS flag.
        self.headed_count = max(0, min(headed_count, len(self.creds)))
        self.sessions:    dict[str, AgentSession] = {}
        self._pw          = None
        self._browser: Browser = None           # headless pool (or the single browser)
        self._browser_headed: Browser = None     # visible browser for headed agents
        self._headless_pool: list[Browser] = []  # headless Chromium shards
        self._browsers:      list[Browser] = []  # all launched browsers (for cleanup)
        self._running     = False
        self._tasks:      list[asyncio.Task] = []

    async def start(self):
        db.init()
        for c in self.creds:
            db.upsert_agent(c["extension"], c["email"], c.get("name", c["email"]))

        launch_args = ["--no-sandbox", "--disable-dev-shm-usage",
                       "--use-fake-ui-for-media-stream",
                       # Real mic: auto-accept the getUserMedia prompt (fake-UI)
                       # but feed the actual hardware microphone (no fake-device).
                       "--disable-web-security"]

        self._pw = await async_playwright().start()

        n_total    = len(self.creds)
        n_headed   = self.headed_count
        n_headless = n_total - n_headed

        # Visible browser for the first `headed_count` agents.
        if n_headed > 0:
            self._browser_headed = await self._pw.chromium.launch(
                headless=False, args=launch_args)
            self._browsers.append(self._browser_headed)

        # Shard the remaining agents across several Chromium processes. One
        # process hosting ~100 contexts saturates and goes unresponsive (dropped
        # rings / missed calls); CONTEXTS_PER_BROWSER contexts per shard keeps
        # each process healthy. When headed_count==0 the pool honours the per-run
        # config.HEADLESS flag (so an all-headed run is still sharded).
        if n_headless > 0:
            pool_headless = True if n_headed > 0 else config.HEADLESS
            shards = max(1, math.ceil(n_headless / CONTEXTS_PER_BROWSER))
            for _ in range(shards):
                br = await self._pw.chromium.launch(
                    headless=pool_headless, args=launch_args)
                self._headless_pool.append(br)
                self._browsers.append(br)
            self._browser = self._headless_pool[0]   # handle for the watchdog
        elif self._browser_headed:
            self._browser = self._browser_headed

        self._running = True
        log.info("Browsers up | %d headed + %d across %d shard(s) | %d agents | ramp=%s",
                 n_headed, n_headless, len(self._headless_pool), n_total,
                 self.ramp_profile)

        sem = asyncio.Semaphore(self.max_workers)
        ramp = RampController(
            len(self.creds), self.ramp_profile, self.ramp_duration,
            broadcast=self.broadcast
        )

        def _browser_for(i):
            # First `headed_count` agents → visible browser; rest → round-robin
            # across the headless shard pool.
            if n_headed > 0 and i < n_headed:
                return self._browser_headed
            return self._headless_pool[(i - n_headed) % len(self._headless_pool)]

        for i, cred in enumerate(self.creds):
            sess = AgentSession(cred, _browser_for(i), self._on_event, self.hold)
            self.sessions[cred["extension"]] = sess

        delays = ramp.delays()
        async def _launch(cred, delay):
            if delay > 0:
                await asyncio.sleep(delay)
            sess = self.sessions[cred["extension"]]
            # Throttle only the heavy bring-online phase (login + SIP register +
            # queue join). Releasing the permit before the call loop means ALL
            # agents answer concurrently — max_workers caps launch rate, not the
            # number of live agents.
            async with sem:
                online = await sess.start()
            if online:
                await sess.run()

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
        for br in self._browsers:
            try:
                await br.close()
            except Exception:
                pass
        if self._pw:
            await self._pw.stop()
        log.info("Runner stopped.")

    async def end_all_calls(self) -> int:
        """Hang up every currently-active call across all agents.

        Targets only sessions on a call (status 'on_call' or 'ringing'); agents
        stay online and available afterwards. Returns the number hung up.
        """
        targets = [s for s in self.sessions.values()
                   if s.status in ("on_call", "ringing")]
        if not targets:
            return 0
        results = await asyncio.gather(*(s.end_call() for s in targets),
                                       return_exceptions=True)
        n = sum(1 for r in results if r is True)
        log.info("End-all-calls: hung up %d of %d active", n, len(targets))
        return n

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
