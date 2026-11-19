"""Load, validate and re-fetch the agent credentials.

Primary path is a DIRECT API login to POST {PORTAL_URL}/api/v1/auth/login.
The endpoint returns clean JSON (not 405/HTML as previously assumed); it just
requires a ``device_type`` field (one of A/I/W/D — we send "W" for Web).

Why not the browser: headless Chromium hangs on launch in some host envs, and
driving 100 headed logins is slow and fragile. The API path is fast and
reliable. A Playwright browser-login fallback is kept at the bottom of this
module but is no longer used by fetch().

Rate limiting is REAL and per-IP: the portal allows ~10 auth requests per
window and returns HTTP 429 with a ``Retry-After`` header when exhausted. We
honour that header and save creds incrementally so a long run is resumable.
"""
import json
import time
import asyncio
import logging
from pathlib import Path

import aiohttp

from config import PORTAL_URL, AGENT_WEB_PASS

log = logging.getLogger(__name__)

_LOGIN_URL = f"{PORTAL_URL}/api/v1/auth/login"
# Authoritative email -> extension/password mapping (ext 1000-9999, shared pw).
_MEMBERS_FILE = "/home/crown/Downloads/members-credentials.json"
# Legacy browser-login pacing (unused by the API path, kept for the fallback).
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


def _ext_map() -> dict:
    """email -> (extension, password) from the authoritative members file."""
    try:
        data = json.loads(Path(_MEMBERS_FILE).read_text())
        rows = data if isinstance(data, list) else (
            data.get("members") or data.get("creds") or [])
        return {r["email"]: (str(r.get("extension", "")), r.get("password", ""))
                for r in rows if r.get("email")}
    except Exception as e:
        log.warning("Could not read members file %s: %s", _MEMBERS_FILE, e)
        return {}


def _ext_fallback(email: str) -> str:
    """Derive extension from +N index: +1 -> 1000, +N -> 999+N."""
    import re
    m = re.search(r'\+(\d+)@', email)
    return str(999 + int(m.group(1))) if m else ""


async def _login_one_api(session: aiohttp.ClientSession, email: str,
                         password: str, max_retries: int = 5) -> dict | None:
    """POST a single login to the portal API. Returns cred dict or None.

    Honours HTTP 429 Retry-After by sleeping and retrying within max_retries.
    """
    payload = {"email": email, "password": password, "device_type": "W"}
    headers = {"Content-Type": "application/json",
               "Origin": PORTAL_URL, "Referer": f"{PORTAL_URL}/login"}
    for attempt in range(max_retries):
        try:
            async with session.post(_LOGIN_URL, json=payload, headers=headers) as resp:
                if resp.status == 429:
                    wait = int(resp.headers.get("Retry-After", "30")) + 1
                    log.warning("429 for %s — waiting %ss (attempt %d/%d)",
                                email, wait, attempt + 1, max_retries)
                    await asyncio.sleep(wait)
                    continue
                body = await resp.json(content_type=None)
                if resp.status == 200 and body.get("success"):
                    msg = body.get("data", {}).get("message", {}) or {}
                    tok = msg.get("token") or body.get("data", {}).get("token")
                    if not tok:
                        log.warning("No token in success body for %s", email)
                        return None
                    name = f"{msg.get('first_name','')} {msg.get('last_name','')}".strip() \
                        or email.split('@')[0]
                    return {"email": email, "token": tok, "name": name}
                err = (body.get("error", {}) or {}).get("message", body) \
                    if isinstance(body, dict) else body
                log.warning("Login rejected %s: %s", email, err)
                return None
        except Exception as e:
            log.error("Login error %s: %s", email, e)
            await asyncio.sleep(2)
    log.error("Gave up on %s after %d retries", email, max_retries)
    return None


async def fetch(
    emails: list[str],
    password: str = AGENT_WEB_PASS,
    out_path: str | None = None,
    progress_cb=None,
) -> list[dict]:
    """Login all emails via the portal API and cache to out_path.

    Reads extension/password from the members file when available, honours the
    portal's per-IP rate limit, and writes out_path incrementally (every few
    creds) so a long rate-limited run is resumable and the live dashboard can
    pick up partial progress.
    """
    extmap = _ext_map()
    creds: list[dict] = []
    timeout = aiohttp.ClientTimeout(total=30)
    connector = aiohttp.TCPConnector(ssl=False, limit=4)

    def _save():
        if out_path and creds:
            Path(out_path).write_text(
                json.dumps({"ts": time.time(), "creds": creds}, indent=2))

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        for i, email in enumerate(emails):
            ext, pw = extmap.get(email, ("", ""))
            cred = await _login_one_api(session, email, pw or password)
            if cred:
                cred["password"]  = pw or password
                cred["extension"] = ext or _ext_fallback(email)
                creds.append(cred)
                log.info("Fetched %d/%d: %s ext=%s",
                         i + 1, len(emails), email, cred["extension"])
            else:
                log.warning("Failed %d/%d: %s", i + 1, len(emails), email)

            if progress_cb:
                await progress_cb(i + 1, len(emails), email, bool(cred))
            if i % 5 == 0:
                _save()

    _save()
    if out_path:
        log.info("Saved %d/%d creds → %s", len(creds), len(emails), out_path)
    return creds


async def _fetch_via_browser(
    emails: list[str],
    password: str = AGENT_WEB_PASS,
    out_path: str | None = None,
    progress_cb=None,
) -> list[dict]:
    """FALLBACK: sequentially login all emails via headless browser.

    Unused by default (headless Chromium can hang on launch in some host envs).
    """
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
