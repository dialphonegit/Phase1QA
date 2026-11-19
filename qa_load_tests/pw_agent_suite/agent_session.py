"""Single-agent Playwright session: login → set online → answer calls → screenshot on events."""
import asyncio
import base64
import logging
import os
import time
from playwright.async_api import Browser, BrowserContext, Page, Error as PWError
from config import PORTAL_URL, SEL, BROWSER_TIMEOUT, HOLD_SECONDS, ANSWER_TIMEOUT, AGENT_WEB_PASS

log = logging.getLogger(__name__)

SCREENSHOTS_DIR = os.getenv("SCREENSHOTS_DIR", "/tmp/pw_screenshots")
CAPTURE_SCREENS = os.getenv("CAPTURE_SCREENS", "true").lower() == "true"


class AgentSession:
    def __init__(self, cred: dict, browser: Browser, on_event=None, hold: int = HOLD_SECONDS):
        self.cred     = cred
        self.browser  = browser
        self.on_event = on_event
        self.hold     = hold
        self.ctx: BrowserContext = None
        self.page: Page          = None
        self.status   = "offline"
        self.answered = 0
        self._running = False

    @property
    def ext(self):  return self.cred.get("extension", "?")
    @property
    def email(self): return self.cred.get("email", "")

    async def _emit(self, event: str, data: dict = None):
        if self.on_event:
            try: await self.on_event(self.ext, event, data or {})
            except Exception: pass

    # ── lifecycle ──────────────────────────────────────────────────────────────

    async def start(self):
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        self.ctx = await self.browser.new_context(
            ignore_https_errors=True,
            permissions=["microphone", "camera"],
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        self.ctx.set_default_timeout(BROWSER_TIMEOUT)
        self.page = await self.ctx.new_page()
        self._running = True

        try:
            await self._login()
            await self._go_online()
            await self._call_loop()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("[%s] fatal: %s", self.ext, e)
            await self._screenshot("error")
            await self._emit("error", {"message": str(e)})
        finally:
            await self._cleanup()

    async def stop(self):
        self._running = False

    async def _cleanup(self):
        self.status = "offline"
        await self._emit("offline")
        try:
            if self.ctx: await self.ctx.close()
        except Exception: pass

    # ── login ──────────────────────────────────────────────────────────────────

    async def _login(self):
        token = self.cred.get("token")
        if token:
            await self.page.goto(f"{PORTAL_URL}/login", wait_until="domcontentloaded")
            # The portal stores the auth JWT under localStorage key "web-token".
            await self.page.evaluate(f"""() => {{
                const t={repr(token)};
                localStorage.setItem('web-token', t);
                localStorage.setItem('lang', 'en');
            }}""")
            await self.page.goto(f"{PORTAL_URL}/dashboard", wait_until="domcontentloaded")
            await self.page.wait_for_timeout(4000)  # let the SPA hydrate + SIP connect
            if "/login" not in self.page.url:
                await self._dismiss_modals()
                await self._emit("logged_in", {"method": "token"})
                return

        await self.page.goto(f"{PORTAL_URL}/login", wait_until="domcontentloaded")
        await self._screenshot("login")
        await self.page.fill(SEL["email"], self.email)
        await self.page.fill(SEL["password"], AGENT_WEB_PASS)
        async with self.page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
            await self.page.click(SEL["submit"])
        await self._dismiss_modals()
        await self._emit("logged_in", {"method": "form"})

    async def _dismiss_modals(self):
        """Dismiss any welcome/onboarding popups that block interaction."""
        for _ in range(3):
            try:
                btn = await self.page.query_selector(SEL["dismiss_modal"])
                if btn:
                    await btn.click()
                    await asyncio.sleep(0.3)
            except Exception:
                pass
            try:
                await self.page.keyboard.press("Escape")
                await asyncio.sleep(0.2)
            except Exception:
                pass

    # ── set online ─────────────────────────────────────────────────────────────

    async def _go_online(self):
        try:
            # Wait for status button (aria-label contains "Your status")
            await self.page.wait_for_selector(SEL["status_btn"], timeout=15000)

            # If already Idle/Active, skip
            aria = await self.page.get_attribute(SEL["status_btn"], "aria-label") or ""
            if "idle" in aria.lower() or "active" in aria.lower():
                self.status = "idle"
                await self._emit("online", {"status": "idle", "already": True})
                return

            # Use JS click to bypass any overlay
            await self.page.evaluate(
                "() => { const b = document.querySelector('button[aria-label*=\"Your status\"]'); if(b) b.click(); }"
            )
            await asyncio.sleep(1)

            # Click "Active ✓" in the popover
            opt = await self.page.wait_for_selector(SEL["online_opt"], timeout=5000)
            await opt.click()
            await asyncio.sleep(0.5)
            self.status = "idle"
        except Exception as e:
            log.warning("[%s] set-online failed: %s", self.ext, e)
            # Try Alt+A keyboard shortcut as fallback
            try:
                await self.page.keyboard.press("Alt+a")
                await asyncio.sleep(0.5)
                self.status = "idle"
            except Exception:
                pass
        await self._emit("online", {"status": self.status})

    # ── call loop ──────────────────────────────────────────────────────────────

    async def _call_loop(self):
        """Poll for incoming calls using multiple detection strategies."""
        while self._running:
            ring_at = None
            try:
                # Strategy 1: wait for known incoming call selectors
                # Strategy 2: watch for any new buttons with phone/answer text
                # Strategy 3: listen for DOM mutations that add call widgets
                incoming = await self._wait_for_incoming(timeout_s=ANSWER_TIMEOUT)
                if not incoming:
                    await self._emit("waiting")
                    continue

                ring_at = time.time()
                await self._screenshot("ringing")
                await self._emit("ringing", {"ring_at": ring_at})

                answered = await self._answer_call()
                if not answered:
                    await self._emit("missed", {"ring_at": ring_at})
                    await asyncio.sleep(2)
                    continue

                answer_at = time.time()
                self.status  = "on_call"
                self.answered += 1
                await self._screenshot("answered")
                await self._emit("answered", {"ring_at": ring_at,
                                              "answer_at": answer_at,
                                              "count": self.answered})
                await asyncio.sleep(self.hold)
                await self._hangup()

                end_at = time.time()
                self.status = "idle"
                await self._emit("call_ended", {
                    "ring_at": ring_at, "answer_at": answer_at,
                    "end_at": end_at, "duration": int(end_at - answer_at),
                })

            except asyncio.CancelledError:
                raise
            except PWError as e:
                if "closed" in str(e).lower():
                    break
                log.warning("[%s] pw: %s", self.ext, e)
                if ring_at:
                    await self._emit("missed", {"ring_at": ring_at})
                await asyncio.sleep(2)
            except Exception as e:
                log.warning("[%s] call loop error: %s", self.ext, e)
                await asyncio.sleep(2)

    async def _wait_for_incoming(self, timeout_s: int = 60) -> bool:
        """
        Wait for an incoming call notification to appear.
        Uses multiple fallback selector strategies since the portal renders
        call widgets dynamically from lazy-loaded JS chunks.
        Returns True if incoming call detected, False on timeout.
        """
        # Primary selectors (order: most specific → broadest fallback)
        primary_sels = SEL["incoming"].split(", ")

        # Broad fallbacks: any new button with answer/accept text
        broad_sels = [
            'button:has-text("Answer")',
            'button:has-text("Accept")',
            '[aria-label*="Answer" i]',
            '[aria-label*="Incoming" i]',
            # Toastify toast that contains a phone action
            '.Toastify__toast button',
            # Any fixed-position overlay with a call action
            '[class*="call"][class*="widget"]',
            '[class*="call"][class*="panel"]',
            '[class*="ring"]',
        ]

        all_sels = primary_sels + broad_sels
        deadline = time.time() + timeout_s

        while self._running and time.time() < deadline:
            for sel in all_sels:
                try:
                    el = await self.page.query_selector(sel)
                    if el and await el.is_visible():
                        log.debug("[%s] incoming detected via: %s", self.ext, sel)
                        return True
                except Exception:
                    pass
            await asyncio.sleep(0.5)

        return False

    async def _answer_call(self) -> bool:
        """Click the answer button using multiple fallback strategies."""
        answer_sels = [
            'button[aria-label*="Answer" i]',
            'button:has-text("Answer")',
            'button:has-text("Accept")',
            '.Toastify__toast button:has-text("Answer")',
            '.Toastify__toast button:has-text("Accept")',
            '[data-testid="answer-btn"]',
        ]
        for sel in answer_sels:
            try:
                btn = await self.page.wait_for_selector(sel, timeout=3000)
                if btn:
                    await btn.click()
                    return True
            except Exception:
                pass

        # Last resort: keyboard shortcut (some portals support Enter to answer)
        try:
            await self.page.keyboard.press("Enter")
            return True
        except Exception:
            pass
        return False

    async def _hangup(self):
        """Hang up the call using multiple fallback strategies."""
        hangup_sels = [
            'button[aria-label*="End" i]',
            'button[aria-label*="Hang" i]',
            'button:has-text("End call")',
            'button:has-text("Hang up")',
            'button:has-text("Hangup")',
            '[data-testid="hangup-btn"]',
        ]
        for sel in hangup_sels:
            try:
                btn = await self.page.wait_for_selector(sel, timeout=3000)
                if btn:
                    await btn.click()
                    return
            except Exception:
                pass

    # ── screenshot ─────────────────────────────────────────────────────────────

    async def _screenshot(self, label: str):
        if not CAPTURE_SCREENS:
            return
        try:
            ts = int(time.time())
            path = f"{SCREENSHOTS_DIR}/{self.ext}_{label}_{ts}.png"
            await self.page.screenshot(path=path, full_page=False)
            # Also emit as base64 for live dashboard preview
            data = open(path, "rb").read()
            b64 = base64.b64encode(data).decode()
            await self._emit("screenshot", {"label": label, "b64": b64, "path": path})
        except Exception:
            pass
