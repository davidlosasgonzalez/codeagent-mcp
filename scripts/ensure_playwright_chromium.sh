#!/usr/bin/env bash
# Install/verify Playwright Chromium for CodeAgent and any extra project venvs.
# Idempotent. Run after uv sync or a server rebuild.
#
# Extra venvs are opt-in, as a colon-separated list of paths:
#   EXTRA_VENVS=/srv/myapp/.venv:/srv/other/.venv bash scripts/ensure_playwright_chromium.sh
set -euo pipefail

BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/var/lib/codeagent-mcp/playwright}"
CODEAGENT_VENV="${CODEAGENT_VENV:-/opt/codeagent-mcp/.venv}"
EXTRA_VENVS="${EXTRA_VENVS:-}"

install -d -m 0750 -o codeagent-mcp -g codeagent-mcp "$BROWSERS_PATH"

install_one() {
  local venv=$1
  if [[ ! -x "$venv/bin/playwright" ]]; then
    echo "SKIP: $venv — no playwright binary"
    return 0
  fi
  echo "ENSURE: $venv → PLAYWRIGHT_BROWSERS_PATH=$BROWSERS_PATH"
  sudo -u codeagent-mcp env PLAYWRIGHT_BROWSERS_PATH="$BROWSERS_PATH" \
    "$venv/bin/playwright" install chromium
}

install_one "$CODEAGENT_VENV"

if [[ -n "$EXTRA_VENVS" ]]; then
  while IFS= read -r venv; do
    [[ -n "$venv" ]] && install_one "$venv"
  done < <(tr ':' '\n' <<<"$EXTRA_VENVS")
fi

# Quick probe with the CodeAgent venv
sudo -u codeagent-mcp env PLAYWRIGHT_BROWSERS_PATH="$BROWSERS_PATH" \
  "$CODEAGENT_VENV/bin/python" - <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
    browser.close()
print("PASS: chromium launch OK")
PY
