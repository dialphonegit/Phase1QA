"""
Auto-discover working CSS selectors for the portal.
Runs headless, screenshots each step, saves selectors.json.

Usage:  python3 selector_probe.py
        python3 selector_probe.py --email shreyanshdubey1122+2@gmail.com
"""
import asyncio
import json
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(__file__))
from config import PORTAL_URL, AGENT_WEB_PASS

OUT_DIR = "/tmp/pw_probe"

CANDIDATES = {
    "email":    ['input[type="email"]', 'input[name="email"]', '#email',
                 'input[placeholder*="email" i]'],
    "password": ['input[type="password"]', 'input[name="password"]', '#password'],
    "submit":   ['button[type="submit"]', 'button:has-text("Login")',
                 'button:has-text("Sign in")', 'input[type="submit"]'],
    "status_btn": ['button:has-text("Phone offline")', 'button:has-text("Idle")',
                   'button:has-text("Online")', '[data-testid="agent-status-btn"]',
                   '[class*="status-btn"]', '[class*="StatusBtn"]'],
    "online_opt": ['li:has-text("Online")', '[data-value="online"]',
                   '[role="option"]:has-text("Online")', 'button:has-text("Online")'],
    "incoming":  ['[class*="incoming"]', '[class*="IncomingCall"]',
                  '[data-testid="incoming-call"]', '[class*="ringing"]',
                  '[class*="Ringing"]', '[aria-label*="incoming" i]'],
    "answer":    ['button:has-text("Answer")', '[data-testid="answer-btn"]',
                  '[aria-label*="answer" i]', 'button[class*="answer" i]'],
    "hangup":    ['button:has-text("End")', 'button:has-text("Hang up")',
                  '[data-testid="hangup"]', '[aria-label*="end call" i]'],
}


async def probe(email: str, password: str = AGENT_WEB_PASS):
    from playwright.async_api import async_playwright
    os.makedirs(OUT_DIR, exist_ok=True)
    found = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        page.set_default_timeout(10000)

        # ── Login page ──────────────────────────────────────────────────────────
        await page.goto(f"{PORTAL_URL}/login", wait_until="domcontentloaded")
        await page.screenshot(path=f"{OUT_DIR}/01_login.png")

        for key in ("email", "password", "submit"):
            found[key] = await _first(page, CANDIDATES[key])
            print(f"  {key:12s} → {found[key] or 'NOT FOUND'}")

        # ── Try login ───────────────────────────────────────────────────────────
        if found.get("email") and found.get("password") and found.get("submit"):
            await page.fill(found["email"], email)
            await page.fill(found["password"], password)
            try:
                async with page.expect_navigation(wait_until="networkidle", timeout=15000):
                    await page.click(found["submit"])
            except Exception as e:
                print(f"  [nav] {e}")
        await page.screenshot(path=f"{OUT_DIR}/02_post_login.png")

        # ── Dashboard selectors ─────────────────────────────────────────────────
        for key in ("status_btn", "online_opt", "incoming", "answer", "hangup"):
            found[key] = await _first(page, CANDIDATES[key])
            print(f"  {key:12s} → {found[key] or 'NOT FOUND'}")

        # ── Dump DOM snippets ───────────────────────────────────────────────────
        for sel, label in [("header", "header"), ("[class*='phone']", "phone_widget")]:
            try:
                el = await page.query_selector(sel)
                if el:
                    html = (await el.inner_html())[:6000]
                    open(f"{OUT_DIR}/{label}.html", "w").write(html)
            except Exception:
                pass

        await page.screenshot(path=f"{OUT_DIR}/03_dashboard.png")
        await browser.close()

    out = json.dumps(found, indent=2)
    open(f"{OUT_DIR}/selectors.json", "w").write(out)
    print(f"\nSelectors saved → {OUT_DIR}/selectors.json")
    print(f"Screenshots    → {OUT_DIR}/")
    return found


async def _first(page, candidates):
    for sel in candidates:
        try:
            el = await page.query_selector(sel)
            if el:
                return sel
        except Exception:
            pass
    return None


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--email",    default="shreyanshdubey1122+2@gmail.com")
    p.add_argument("--password", default=AGENT_WEB_PASS)
    args = p.parse_args()
    print(f"Probing {PORTAL_URL} as {args.email}…")
    result = asyncio.run(probe(args.email, args.password))
    print(json.dumps(result, indent=2))
