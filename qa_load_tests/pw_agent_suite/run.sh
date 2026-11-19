#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "=== PW Agent Suite Setup ==="

# Install Python deps
pip install -r requirements.txt -q --break-system-packages

# Install Playwright Chromium
python3 -m playwright install chromium --with-deps 2>&1 | tail -5

echo ""
echo "=== Starting server on http://0.0.0.0:${SERVER_PORT:-8765} ==="
echo "Open the dashboard in your browser after the preview URL appears."
echo ""
exec python3 server.py
