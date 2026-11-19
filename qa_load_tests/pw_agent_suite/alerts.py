"""Alert engine — monitors live stats and fires threshold-based alerts."""
import asyncio
import time
import logging
from dataclasses import dataclass, field
from typing import Callable, Awaitable
import db

log = logging.getLogger(__name__)

POLL_INTERVAL = 10  # seconds


@dataclass
class AlertRule:
    name:      str
    metric:    str          # "asr" | "avg_dur" | "agents_online" | "missed_per_min"
    op:        str          # "lt" | "gt"
    threshold: float
    severity:  str = "warn" # "warn" | "critical"
    cooldown:  int = 60     # seconds between re-firing same alert


DEFAULT_RULES = [
    AlertRule("Low answer rate",    "asr",            "lt", 70.0,  "critical", 120),
    AlertRule("High avg duration",  "avg_dur",        "gt", 120.0, "warn",      60),
    AlertRule("No agents online",   "agents_online",  "lt", 1.0,   "critical",  30),
    AlertRule("Missed surge",       "missed_per_min", "gt", 10.0,  "warn",      60),
]


class AlertEngine:
    def __init__(self, rules: list[AlertRule] = None, broadcast: Callable = None,
                 runner=None):
        self.rules      = rules or DEFAULT_RULES
        self.broadcast  = broadcast
        self.runner     = runner
        self._running   = False
        self._last_fire: dict[str, float] = {}
        self.history:   list[dict] = []

    async def start(self):
        self._running = True
        log.info("AlertEngine started (%d rules)", len(self.rules))
        while self._running:
            await asyncio.sleep(POLL_INTERVAL)
            await self._evaluate()

    async def stop(self):
        self._running = False

    async def _evaluate(self):
        s   = db.stats()
        now = time.time()
        summary = s.get("summary", {}) or {}
        agents  = s.get("agents",  []) or []

        answered   = summary.get("answered") or 0
        missed     = summary.get("missed")   or 0
        total      = answered + missed
        asr        = (answered / total * 100) if total else 100.0
        avg_dur    = summary.get("avg_dur") or 0

        snap = self.runner.snapshot() if self.runner else {}
        agents_online = sum(1 for st in snap.values() if st not in ("offline", "error"))

        # Missed per minute (last minute of calls)
        recent = s.get("recent", [])
        minute_ago = now - 60
        missed_pm = sum(1 for c in recent if c.get("outcome") == "missed"
                        and (c.get("ring_at") or 0) > minute_ago)

        metrics = {
            "asr":           asr,
            "avg_dur":       avg_dur,
            "agents_online": agents_online,
            "missed_per_min": missed_pm,
        }

        for rule in self.rules:
            val = metrics.get(rule.metric, 0)
            triggered = (rule.op == "lt" and val < rule.threshold) or \
                        (rule.op == "gt" and val > rule.threshold)
            if not triggered:
                continue
            last = self._last_fire.get(rule.name, 0)
            if now - last < rule.cooldown:
                continue
            self._last_fire[rule.name] = now
            alert = {
                "name":      rule.name,
                "severity":  rule.severity,
                "metric":    rule.metric,
                "value":     round(val, 1),
                "threshold": rule.threshold,
                "op":        rule.op,
                "ts":        now,
            }
            self.history.insert(0, alert)
            if len(self.history) > 200:
                self.history.pop()
            log.warning("ALERT [%s] %s=%.1f %s %.1f",
                        rule.severity, rule.metric, val, rule.op, rule.threshold)
            if self.broadcast:
                await self.broadcast({"type": "alert", **alert})

    def get_history(self) -> list[dict]:
        return self.history[:50]

    def update_rules(self, rules: list[dict]):
        self.rules = [AlertRule(**r) for r in rules]
