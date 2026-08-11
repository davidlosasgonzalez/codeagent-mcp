# Hardening — Unix user, systemd, HTTP perimeter

**Status:** operational baseline for self-hosted HTTPS deploy.

## Threat model

Shell/MCP as `codeagent-mcp` is **not** VPS administration:

| Capability | State |
|------------|--------|
| Service user | `codeagent-mcp` (system, shell `nologin`, HOME `/var/lib/codeagent-mcp`) |
| sudo / `docker` group | **absent** |
| FastMCP bind | loopback only (e.g. `127.0.0.1:8765`) |
| Public entry | Caddy TLS → loopback |
| Package tree (e.g. `/opt/codeagent-mcp`) | `root:root`, service **read-only** |
| CPython runtime | outside operator HOME (RO; avoids `ProtectHome` traps) |
| Home protection | `ProtectHome=read-only` (required so `/run/user/<uid>` works with linger/tmux; `true` hides `/run/user` even with `ReadWritePaths`) |
| State / OAuth / tmux / artifacts | `/var/lib/codeagent-mcp` (service uid) |
| Secrets | `/etc/codeagent-mcp/http.env` `0640 root:codeagent-mcp` |
| Registered project roots | group + ACL as you choose; write via `writable` / `writable_env` + systemd **`ReadWritePaths=`** for each writable root; keep secrets in `InaccessiblePaths=` when needed |

OAuth authenticates identity; it does **not** elevate Unix privileges. After auth, allowlist `sub`, leases, registry roots, and file permissions still apply.

## Fail-closed (HTTP start)

With `--transport http` and auth on, the process **aborts** if missing:

- `CODEAGENT_GITHUB_CLIENT_ID` / `CODEAGENT_GITHUB_CLIENT_SECRET`
- `CODEAGENT_JWT_SIGNING_KEY` (persistent signing key)
- `CODEAGENT_ALLOWED_SUBS` (non-empty CSV)

Never start with `--no-auth` behind Caddy.

## OAuth keys and state

| Piece | Where |
|-------|--------|
| GitHub client secret | `http.env` |
| JWT signing key | `CODEAGENT_JWT_SIGNING_KEY` in `http.env` |
| Client registrations / encrypted tokens | under the service HOME FastMCP OAuth store |

### Rotation / revocation

1. New key: `openssl rand -hex 32` → update `CODEAGENT_JWT_SIGNING_KEY`.
2. `systemctl restart codeagent-mcp-http` (clients reauth; old JWTs fail).
3. Leaked client secret: rotate GitHub OAuth App + `http.env` + restart.
4. Revoke a human: remove `sub` from `CODEAGENT_ALLOWED_SUBS` + restart.
5. **Forbidden:** recover by disabling OAuth or exposing the loopback port publicly.

## systemd

Canonical unit in repo: `deploy/codeagent-mcp-http.service` → `/etc/systemd/system/`.

Notes:

- `KillMode=process` — dedicated tmux server survives MCP restart.
- `PrivateTmp=yes` — isolates `/tmp`.
- **`TMPDIR=/var/lib/codeagent-mcp/tmp`** (also `TEMP`/`TMP`) — durable tempfile for exec/tmux/pytest; **do not** allowlist `TMPDIR` in `exec_run` (deny-by-default).
- `ProtectSystem=strict` + measured `ReadWritePaths` / `ReadOnlyPaths`.
- Add each **writable** registered root to `ReadWritePaths=` when enabling writes.
- **Do not** blindly enable `PrivateUsers`, `RestrictNamespaces`, or `SystemCallFilter` without revalidating Playwright + `openat2` + tmux.
- Browser is **not** durable across MCP restart (unlike tmux).

## Caddy

Canonical template: `deploy/Caddyfile.example` (copy to the host; do not commit host TLS names into the package tree).

- `request_body max_size 4MB` (Caddy/`go-humanize`: **MB = decimal** → 4_000_000 bytes, not MiB). Coupled with `fs_write_binary` / `fs_write_file` decoded/stream cap `2_000_000` — raise both together.
- File download host allowlist for `fs_write_file`: `CODEAGENT_FILE_DOWNLOAD_HOST_SUFFIXES` (default `oaiusercontent.com,openai.com,chatgpt.com`). Adaptation-only; not a generic URL fetch.
- Dial/response timeouts toward the upstream
- Access log filter that strips `Authorization` / `Cookie` / `X-Api-Key`
- Extra rate-limit modules: deferred unless your Caddy build includes them

## Recovery

```bash
# App only (tmux panes should survive)
systemctl restart codeagent-mcp-http

# Proxy only
systemctl reload caddy   # or restart if reload insufficient

# Verify anonymous still rejected
curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://mcp.example.com/mcp/ \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
# expect 401
```

Threat tests: `scripts/threat_tests.sh` (run as root on the host). Set
`TARGET_APP_ROOT` and `TARGET_WRITE_ENV` to a registered writable checkout and
its gate variable to include the write-gate probe; it is skipped otherwise.

## Beyond this baseline

Structured per-tool audit, spool/artifact quotas, orphan session ops → implemented as ops/audit tools (see [`../product/ops.md`](../product/ops.md)).

## Deploy note (chmod)

After `chown root:root` on the package tree, **do not** `chmod 644` everything under `.venv`: preserve `+x` on `.venv/bin/*`, `playwright/driver/node`, and `*.so`. The unit uses `ExecStart=.venv/bin/codeagent-mcp` (no `uv run` at runtime).

## Gotcha: RestrictSUIDSGID vs openat2

`RestrictSUIDSGID=true` installs a seccomp filter that returns **ENOSYS (errno=38)** for `openat2`.
PathJail then fails every `fs_*` / instruction body read. **Do not enable** on `codeagent-mcp-http`.
