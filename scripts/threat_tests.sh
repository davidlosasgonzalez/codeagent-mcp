#!/usr/bin/env bash
# Threat tests — run as root on the CodeAgent host.
# Does not modify target project contents.
set -euo pipefail

PASS=0
FAIL=0
pass() { echo "PASS  $*"; PASS=$((PASS + 1)); }
fail() { echo "FAIL  $*"; FAIL=$((FAIL + 1)); }

# Fall back to the deployed service env when not exported in this shell.
if [[ -z "${CODEAGENT_HOST:-}" && -r /etc/codeagent-mcp/http.env ]]; then
  CODEAGENT_HOST=$(grep -E '^CODEAGENT_HOST=' /etc/codeagent-mcp/http.env | cut -d= -f2- | tr -d '"')
fi
HOST_PUBLIC="${CODEAGENT_HOST:-mcp.example.com}"
LOOPBACK_PORT="${CODEAGENT_HTTP_PORT:-8765}"
# Writable target checkout probed by the write-gate check (skipped if absent),
# and the writable_env gate that is expected to control it.
TARGET_APP_ROOT="${TARGET_APP_ROOT:-/srv/example-app}"
TARGET_WRITE_ENV="${TARGET_WRITE_ENV:-CODEAGENT_MYAPP_WRITE}"

echo "=== threat tests ==="

# Wait for loopback bind (startup cleanup can delay listen)
for i in $(seq 1 30); do
  if ss -ltn | grep -qE "127\\.0\\.0\\.1:${LOOPBACK_PORT}\\b"; then
    break
  fi
  sleep 0.5
done


# 1) Internal port not reachable from a non-loopback local bind check via ss
if ss -ltn | grep -qE "127\\.0\\.0\\.1:${LOOPBACK_PORT}\\b"; then
  pass "FastMCP listens on 127.0.0.1:${LOOPBACK_PORT}"
else
  fail "FastMCP not listening on loopback :${LOOPBACK_PORT}"
fi
if ss -ltn | grep -qE "0\\.0\\.0\\.0:${LOOPBACK_PORT}\\b|\\*:${LOOPBACK_PORT}\\b"; then
  fail "FastMCP appears bound on non-loopback :${LOOPBACK_PORT}"
else
  pass "FastMCP not bound on wildcard :${LOOPBACK_PORT}"
fi

# 2) External-ish: curl public host anonymous MCP → 401
code=$(curl -sS -o /tmp/codeagent_threat_anon.json -w '%{http_code}' -X POST "https://${HOST_PUBLIC}/mcp/" \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' || true)
if [[ "$code" == "401" || "$code" == "403" ]]; then
  pass "anonymous https://${HOST_PUBLIC}/mcp/ → ${code}"
else
  fail "anonymous https://${HOST_PUBLIC}/mcp/ → ${code} (expected 401/403)"
fi

# 3) OAuth discovery alive
disc=$(curl -sS -o /dev/null -w '%{http_code}' "https://${HOST_PUBLIC}/.well-known/oauth-authorization-server" || true)
if [[ "$disc" == "200" ]]; then
  pass "oauth discovery 200"
else
  fail "oauth discovery → ${disc}"
fi

# 4) User groups / sudo / docker
if id -nG codeagent-mcp | tr ' ' '\n' | grep -Eq '^(docker|sudo)$'; then
  fail "codeagent-mcp in docker/sudo group"
else
  pass "codeagent-mcp not in docker/sudo"
fi
if sudo -n -u codeagent-mcp sudo -n true 2>/dev/null; then
  fail "codeagent-mcp can sudo"
else
  pass "codeagent-mcp cannot sudo"
fi
if sudo -u codeagent-mcp test -r /var/run/docker.sock 2>/dev/null; then
  fail "codeagent-mcp can read docker.sock"
else
  pass "codeagent-mcp cannot read docker.sock"
fi

# 5) Sensitive paths
if sudo -u codeagent-mcp test -r /etc/shadow; then
  fail "codeagent-mcp can read /etc/shadow"
else
  pass "codeagent-mcp cannot read /etc/shadow"
fi
if sudo -u codeagent-mcp test -r /root; then
  fail "codeagent-mcp can traverse /root"
else
  pass "codeagent-mcp cannot read /root"
fi
# Directory `test -w` is unreliable with ACLs; probe a real file via Python.
# Point TARGET_APP_ROOT at a registered writable checkout to enable this check.
probe_file="$TARGET_APP_ROOT/AGENTS.md"
if [[ ! -d "$TARGET_APP_ROOT" ]]; then
  echo "SKIP  write-gate probe: $TARGET_APP_ROOT does not exist (set TARGET_APP_ROOT)"
elif grep -q "^${TARGET_WRITE_ENV}=1" /etc/codeagent-mcp/http.env 2>/dev/null; then
  if sudo -u codeagent-mcp python3 -c "import os; raise SystemExit(0 if os.access('$probe_file', os.W_OK) else 1)"; then
    pass "codeagent-mcp can write $TARGET_APP_ROOT files (write gate on, ACL probe)"
  else
    fail "${TARGET_WRITE_ENV}=1 but $TARGET_APP_ROOT file not writable by codeagent-mcp"
  fi
else
  if sudo -u codeagent-mcp python3 -c "import os; raise SystemExit(0 if os.access('$probe_file', os.W_OK) else 1)"; then
    fail "codeagent-mcp can write $TARGET_APP_ROOT while ${TARGET_WRITE_ENV}!=1"
  else
    pass "codeagent-mcp cannot write $TARGET_APP_ROOT (write gate off)"
  fi
fi
if sudo -u codeagent-mcp test -w /opt/codeagent-mcp/pyproject.toml; then
  fail "codeagent-mcp can write service code tree"
else
  pass "codeagent-mcp cannot write service code tree"
fi

# 6) No personal SSH keys in HOME
if sudo -u codeagent-mcp bash -lc 'test -e "$HOME/.ssh/id_rsa" -o -e "$HOME/.ssh/id_ed25519"'; then
  fail "personal SSH private keys present in HOME"
else
  pass "no personal SSH private keys in HOME"
fi

# 7) Restart window: anonymous must not get tool 200
bad=0
(
  for _ in $(seq 1 40); do
    c=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "https://${HOST_PUBLIC}/mcp/" \
      -H 'content-type: application/json' \
      -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' || echo 000)
    # During restart, connection errors (000/502/503) are OK; 200 is not.
    if [[ "$c" == "200" ]]; then
      echo "$c" > /tmp/codeagent_threat_restart_bad
    fi
    sleep 0.25
  done
) &
probe=$!
systemctl restart codeagent-mcp-http
sleep 2
wait "$probe" || true
if [[ -f /tmp/codeagent_threat_restart_bad ]]; then
  fail "anonymous got HTTP 200 during/after restart"
  rm -f /tmp/codeagent_threat_restart_bad
else
  pass "no anonymous 200 during codeagent restart window"
fi
systemctl is-active --quiet codeagent-mcp-http && pass "service active after restart" || fail "service not active after restart"

# 8) Log scrub — journal must not contain known secret markers from env file keys
# (values themselves are not printed here). Look for literal header name dumps.
if journalctl -u codeagent-mcp-http -n 200 --no-pager | grep -Ei 'authorization:[[:space:]]*bearer|CODEAGENT_GITHUB_CLIENT_SECRET=|CODEAGENT_JWT_SIGNING_KEY='; then
  fail "journal appears to contain secrets or Authorization bearer"
else
  pass "journal sample has no obvious secret/Authorization dumps"
fi

echo "=== result: ${PASS} pass, ${FAIL} fail ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
