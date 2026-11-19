"""Load, validate and re-fetch the 100-agent credentials via browser login.

The portal /auth/login REST endpoint is rate-limited and returns 405/HTML
when hit via API client. We use Playwright headless browser login instead,
which also captures the JWT from localStorage after a successful form submit.
"""
import json
import time
import asyncio
import logging
from pathlib import Path
from config import PORTAL_URL, AGENT_WEB_PASS

log = logging.getLogger(__name__)

# Portal rate-limits ~5 logins per 68s. 14s gap = safe for sustained fetch.
_LOGIN_INTERVAL = 14.0


def load(path: str) -> list[dict]:
    """Return list of cred dicts from a cached file, or [] if missing."""
    p = Path(path)
    if not p.exists():
        return []
    data = json.loads(p.read_text())
    creds = data.get("creds", [])
    ts = data.get("ts", 0)
    if time.time() - ts > 72_000:
        log.warning("Creds file is stale (>20h old) — consider re-fetching")
    return creds


async def _login_one_browser(browser, email: str, password: str) -> dict | None:
    """Login via headless browser, intercept /api/v1/auth/login response for JWT."""
    import re as _re
    ctx = await browser.new_context(ignore_https_errors=True)
    page = await ctx.new_page()
    captured: dict = {}

    async def _on_response(resp):
        if '/api/v1/auth/login' in resp.url and resp.status == 200:
            try:
                body = await resp.json()
                tok = (body.get("data", {}).get("message", {}).get("token")
                       or body.get("data", {}).get("token")
                       or body.get("token"))
                if tok:
                    captured["token"] = tok
                    # Also grab user info
                    user = (body.get("data", {}).get("message", {})
                            or body.get("data", {}).get("user", {}))
                    captured["name"] = user.get("name", "")
                    captured["extension"] = str(user.get("extension", ""))
            except Exception:
                pass

    page.on("response", _on_response)

    try:
        await page.goto(f"{PORTAL_URL}/login", wait_until="networkidle", timeout=30000)
        await page.fill('#Email', email)
        await page.fill('#Password', password)
        await page.click('button[type="submit"]:not(:has-text("Debug"))')

        # Wait for the auth API response (up to 15s)
        for _ in range(30):
            if "token" in captured:
                break
            await asyncio.sleep(0.5)

        if "token" not in captured:
            log.warning("No token captured for %s", email)
            return None

        # Derive extension fallback from email index
        m = _re.search(r'\+(\d+)@', email)
        idx = int(m.group(1)) if m else 1
        extension = captured.get("extension") or str(1000 + idx)
        name = captured.get("name") or email.split('@')[0]

        return {
            "email":     email,
            "token":     captured["token"],
            "extension": extension,
            "name":      name,
            "password":  password,
        }

    except Exception as e:
        log.error("Browser login failed for %s: %s", email, e)
        return None
    finally:
        try:
            await ctx.close()
        except Exception:
            pass


async def fetch(
    emails: list[str],
    password: str = AGENT_WEB_PASS,
    out_path: str | None = None,
    progress_cb=None,
) -> list[dict]:
    """Sequentially login all emails via headless browser and cache to out_path."""
    from playwright.async_api import async_playwright

    creds = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        for i, email in enumerate(emails):
            cred = await _login_one_browser(browser, email, password)
            if cred:
                creds.append(cred)
                log.info("Fetched cred %d/%d: %s ext=%s", i+1, len(emails), email, cred["extension"])
            else:
                log.warning("Failed cred %d/%d: %s", i+1, len(emails), email)

            if progress_cb:
                await progress_cb(i + 1, len(emails), email, bool(cred))

            # Rate-limit gap between logins
            if i < len(emails) - 1:
                await asyncio.sleep(_LOGIN_INTERVAL)

        await browser.close()

    if out_path and creds:
        Path(out_path).write_text(json.dumps({"ts": time.time(), "creds": creds}, indent=2))
        log.info("Saved %d creds → %s", len(creds), out_path)

    return creds


def member_emails(start: int = 1, end: int = 100) -> list[str]:
    return [f"shreyanshdubey1122+{i}@gmail.com" for i in range(start, end + 1)]
