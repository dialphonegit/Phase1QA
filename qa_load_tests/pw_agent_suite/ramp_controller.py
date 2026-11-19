"""Ramp profiles — control how agents come online over time."""
import asyncio
import math
import logging
from typing import Callable, Awaitable

log = logging.getLogger(__name__)


class RampController:
    """
    Wraps AgentRunner.sessions startup with a ramp profile.

    Profiles:
      instant  — all agents start simultaneously (default)
      linear   — spread evenly over ramp_duration seconds
      wave     — sine-curve burst pattern (peaks halfway)
      step     — batch of step_size every step_interval seconds
    """

    def __init__(self, total: int, profile: str = "instant",
                 ramp_duration: int = 60, step_size: int = 10,
                 step_interval: int = 10, broadcast: Callable = None):
        self.total         = total
        self.profile       = profile
        self.ramp_duration = ramp_duration
        self.step_size     = step_size
        self.step_interval = step_interval
        self.broadcast     = broadcast

    def delays(self) -> list[float]:
        """Return per-agent start delay in seconds."""
        n = self.total
        if n == 0:
            return []
        p = self.profile

        if p == "instant":
            return [0.0] * n

        if p == "linear":
            if n == 1:
                return [0.0]
            return [i * self.ramp_duration / (n - 1) for i in range(n)]

        if p == "wave":
            # Agents launch fastest at the midpoint (sine wave)
            delays = []
            cumulative = 0.0
            for i in range(n):
                # Inverse of sine: more agents in the middle
                phase = math.pi * i / n
                interval = (self.ramp_duration / n) * (0.3 + 0.7 * math.sin(phase))
                delays.append(cumulative)
                cumulative += interval
            return delays

        if p == "step":
            delays = []
            for i in range(n):
                batch = i // self.step_size
                delays.append(float(batch * self.step_interval))
            return delays

        return [0.0] * n   # fallback

    async def run(self, tasks: list[Callable[[], Awaitable]]):
        """Launch each task after its computed delay."""
        delays = self.delays()
        log.info("Ramp profile=%s  total=%d  duration=%.0fs",
                 self.profile, self.total, delays[-1] if delays else 0)

        async def _delayed(delay, task_fn):
            if delay > 0:
                await asyncio.sleep(delay)
            if self.broadcast:
                await self.broadcast({"type": "ramp_tick",
                                      "profile": self.profile, "delay": delay})
            await task_fn()

        await asyncio.gather(*[_delayed(d, t) for d, t in zip(delays, tasks)],
                             return_exceptions=True)
