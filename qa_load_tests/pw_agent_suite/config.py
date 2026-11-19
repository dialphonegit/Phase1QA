import os

PORTAL_URL       = os.getenv("PORTAL_URL",       "https://portal.qa.unifiedrtc.com")
ADMIN_EMAIL      = os.getenv("ADMIN_EMAIL",      "shreyanshdubey1122@gmail.com")
ADMIN_PASSWORD   = os.getenv("ADMIN_PASSWORD",   "Aadmin@123456!")
AGENT_WEB_PASS   = os.getenv("AGENT_WEB_PASS",   "Aadmin@123456!")

# Queue to forward DID to (LoadTest-Queue-100)
QUEUE_ID         = os.getenv("QUEUE_ID",         "6afddee65ca1e9ca9af6baf6")
QUEUE_NAME       = os.getenv("QUEUE_NAME",       "LoadTest-Queue-100")
QUEUE_EXT        = os.getenv("QUEUE_EXT",        "0100")

# DID for call forwarding
DID_UUID         = os.getenv("DID_UUID",         "6ad7a56a8f7c416cd1397902")
DID_NUMBER       = os.getenv("DID_NUMBER",       "+1 201 778 2309")

# Browser
HEADLESS         = os.getenv("HEADLESS",         "false").lower() == "true"
MAX_WORKERS      = int(os.getenv("MAX_WORKERS",  "10"))   # concurrent Playwright contexts
BROWSER_TIMEOUT  = int(os.getenv("BROWSER_TIMEOUT", "30000"))  # ms per action

# Call behaviour
NUM_AGENTS       = int(os.getenv("NUM_AGENTS",   "100"))
HOLD_SECONDS     = int(os.getenv("HOLD_SECONDS", "30"))   # hold call before hangup
ANSWER_TIMEOUT   = int(os.getenv("ANSWER_TIMEOUT","60"))  # seconds to wait for ring

# Server
SERVER_HOST      = os.getenv("SERVER_HOST",      "0.0.0.0")
SERVER_PORT      = int(os.getenv("SERVER_PORT",  "8765"))

# Credentials cache
CREDS_FILE       = os.getenv("CREDS_FILE",       "/tmp/queue100_creds.json")

# Portal UI selectors — discovered via selector_probe.py on 2026-11-19
SEL = {
    # Login page — real IDs confirmed
    "email":        '#Email',
    "password":     '#Password',
    # Submit but NOT the debug button
    "submit":       'button[type="submit"]:not(:has-text("Debug"))',

    # Dashboard — dismiss the "Claim Free DID" popup that appears on first load
    "dismiss_modal": 'button:has-text("Maybe later")',

    # Status button: aria-label="Your status: …" — covers Idle/Phone offline/Away etc.
    "status_btn":   'button[aria-label*="Your status"]',

    # After clicking status_btn a popover opens; "Active ✓" = queue calls on
    "online_opt":   '[data-slot="popover-content"] button:has-text("Active")',

    # Incoming call widget — portal shows a Toastify toast or fixed overlay.
    # The toast container is always present; actual ringing call appears as a child.
    # We wait for a button containing a green phone icon (Answer) inside the toast area.
    # Fallback selectors cover possible future portal changes.
    "incoming":     '.Toastify__toast:has(button[aria-label*="nswer" i]), '
                    '.Toastify__toast:has(button:has-text("Answer")), '
                    '[class*="incoming-call"], [class*="IncomingCall"], '
                    '[data-testid="incoming-call"]',

    "answer":       'button[aria-label*="Answer" i], '
                    'button:has-text("Answer"), '
                    '.Toastify__toast button:has-text("Accept"), '
                    '[data-testid="answer-btn"]',

    "hangup":       'button[aria-label*="End" i], '
                    'button[aria-label*="Hang" i], '
                    'button:has-text("End call"), '
                    'button:has-text("Hang up"), '
                    '[data-testid="hangup-btn"]',

    "dashboard":    'a[href="/"], a[href="/dashboard"]',
}
