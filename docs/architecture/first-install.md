# First install — CodeAgent MCP on a new Linux server

Guide for bringing up CodeAgent MCP **from zero** on a dedicated Linux host (Ubuntu + Caddy + systemd + GitHub OAuth + ChatGPT Developer Mode).

Canonical templates live under [`deploy/`](../../deploy/). This page is the ordered checklist; deep rationale is in [`remote-mcp-transport.md`](remote-mcp-transport.md) and [`hardening.md`](hardening.md).

**Not covered here:** publishing the ChatGPT MCP app to a workspace. Keep the connector in **draft** until the tool surface is frozen.

## Starting point: you only have a server IP

Assume the operator can SSH to a VPS **by public IP** and nothing else is set up yet (no hostname, no TLS, no OAuth app, no ChatGPT connector).

### Domain name — only for ChatGPT / public HTTPS

If your clients are **stdio-only** (Cursor, Claude Code, local MCP on the box), skip this subsection — no DNS, Caddy, or OAuth required. See [Optional: stdio-only](#optional-stdio-only-no-domain) below.

The production ChatGPT path is **HTTPS on a hostname**, not bare IP:

- Let's Encrypt / Caddy expect a DNS name.
- GitHub OAuth callback URLs are host-based (`https://<host>/auth/callback`).
- ChatGPT custom MCP connectors expect an `https://…/mcp/` URL.

So before (or while) you finish the steps below, point **some domain you control** at this server's public IP (A/AAAA). **How** you create that record is up to you and your DNS provider — this guide does not document registrar UI. What matters for CodeAgent is:

1. You choose `CODEAGENT_HOST` (e.g. `mcp.example.com`).
2. Public DNS resolves that name to this machine.
3. Port **443** reaches Caddy on this host (firewall/security group allows it).

Until DNS works, you can still do local steps (user, clone, `uv sync`, stdio smoke). You cannot finish OAuth + ChatGPT over the public path on raw IP alone.

### Optional: stdio-only (no domain)

If you only want MCP over stdio on the box (Claude Code / Cursor / local clients), skip domain, Caddy, and OAuth — see the last section. That mode is **not** the ChatGPT remote path.

## What you get (remote path)

```
ChatGPT (or any HTTPS MCP client)
  → HTTPS :443 (Caddy + Let's Encrypt) on your domain
  → FastMCP Streamable HTTP on 127.0.0.1:8765
  → CodeAgent Core (tools under lease + path jail)
```

- **Stdio** remains for local/dev (`uv run codeagent-mcp`).
- Clients never choose filesystem roots; the server registry does (`projects.yaml` / `CODEAGENT_PROJECTS_FILE`).

## 0. Prerequisites

| Need | Notes |
|------|--------|
| Linux VPS | Root SSH; kernel **5.6+** on x86-64/arm64 (`openat2` is mandatory — see [`host-requirements.md`](host-requirements.md)); enough RAM for Chromium (≈2G+ `MemoryMax` on the unit) |
| Domain → this IP | A/AAAA for `CODEAGENT_HOST` (you configure DNS; see "Starting point") |
| GitHub OAuth App | Callback exactly `https://<CODEAGENT_HOST>/auth/callback` |
| Packages | `git`, `curl`, `tmux`, `ripgrep`, `acl` (`setfacl`), Caddy (or equivalent TLS reverse proxy) |
| Python | **3.12** via `uv` (pin `.python-version`) |

Optional later: Playwright Chromium (browser/visual tools), a target git checkout.

## 1. System user and directories

```bash
# Service account (no login shell)
useradd --system --home-dir /var/lib/codeagent-mcp --create-home \
  --shell /usr/sbin/nologin codeagent-mcp

install -d -m 0750 -o codeagent-mcp -g codeagent-mcp /var/lib/codeagent-mcp
install -d -m 0700 -o codeagent-mcp -g codeagent-mcp /var/lib/codeagent-mcp/tmp
install -d -m 0750 -o root -g codeagent-mcp /etc/codeagent-mcp

# Lingering so /run/user/<uid> exists for tmux (required with ProtectHome=read-only)
loginctl enable-linger codeagent-mcp
id -u codeagent-mcp   # remember this UID for XDG_RUNTIME_DIR + systemd ReadWritePaths
```

Do **not** add `codeagent-mcp` to `sudo` or `docker`.

## 2. Install uv + Python 3.12 (outside `/root`)

`ProtectHome=read-only` hides `/root` from the service. Put the managed CPython tree somewhere the unit can mark read-only, e.g. `/opt/uv-python`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# Install a shared CPython (example — adjust to your uv layout):
uv python install 3.12
# Ensure the interpreter used by the venv is under a path listed in ReadOnlyPaths
# (this host uses /opt/uv-python). Symlink or UV_PYTHON_INSTALL_DIR as needed.
```

Also install host tools:

```bash
apt-get update
apt-get install -y git tmux ripgrep acl caddy
```

## 3. Clone CodeAgent and create the venv

```bash
git clone https://github.com/davidlosasgonzalez/codeagent-mcp.git /opt/codeagent-mcp
# Or your fork / mirror.
chown -R root:root /opt/codeagent-mcp
cd /opt/codeagent-mcp
uv sync
# Runtime binary used by systemd (preserve +x on .venv/bin/*):
ls -l .venv/bin/codeagent-mcp
```

Install Playwright browsers **as the service user** into its HOME (after env paths exist):

```bash
sudo -u codeagent-mcp -H bash -lc '
  export PLAYWRIGHT_BROWSERS_PATH=/var/lib/codeagent-mcp/playwright
  cd /opt/codeagent-mcp && .venv/bin/playwright install chromium
  # preferred idempotent helper:
  # bash /opt/codeagent-mcp/scripts/ensure_playwright_chromium.sh
'
```

## 4. Register target projects (access control)

**This is how you decide what the agent may touch on the server.**

Clients never choose filesystem roots. You declare every allowed checkout in YAML:

→ Full guide: [`../product/projects-registry.md`](../product/projects-registry.md)

```bash
cp /opt/codeagent-mcp/deploy/projects.example.yaml /etc/codeagent-mcp/projects.yaml
chmod 0640 /etc/codeagent-mcp/projects.yaml
chown root:codeagent-mcp /etc/codeagent-mcp/projects.yaml
# Edit ids + absolute roots for YOUR apps.
# Ensure CODEAGENT_PROJECTS_FILE=/etc/codeagent-mcp/projects.yaml in http.env
mkdir -p /var/lib/codeagent-mcp/demo-root
chown codeagent-mcp:codeagent-mcp /var/lib/codeagent-mcp/demo-root
```

Create each root directory with permissions the service can read (and write only if you intend it). Restart the unit after edits.

### Optional: writable production checkout

Only if you want controlled writes under lease:

1. Group + ACL (or ownership) so the `codeagent-mcp` user can write the checkout.
2. Enable the project's `writable_env` gate in `http.env` / `projects.yaml`.
3. Add the checkout to systemd `ReadWritePaths=`.
4. Use stable environment-specific project identifiers. Do not rename identifiers to bypass client-side safety controls.

## 5. Secrets and environment

```bash
cp /opt/codeagent-mcp/deploy/http.env.example /etc/codeagent-mcp/http.env
chmod 0640 /etc/codeagent-mcp/http.env
chown root:codeagent-mcp /etc/codeagent-mcp/http.env
```

Fill at least:

| Variable | Value |
|----------|--------|
| `CODEAGENT_HOST` / `CODEAGENT_BASE_URL` | Public hostname / `https://…` |
| `CODEAGENT_GITHUB_CLIENT_ID` / `_SECRET` | From GitHub OAuth App |
| `CODEAGENT_JWT_SIGNING_KEY` | `openssl rand -hex 32` |
| `CODEAGENT_ALLOWED_SUBS` | Temporary: leave a placeholder until first login, **or** put your known `sub` |
| `XDG_RUNTIME_DIR` | `/run/user/<uid>` from `id -u codeagent-mcp` |

HTTP transport is **fail-closed**: missing OAuth/JWT/allowlist → process aborts. Never run `--no-auth` behind Caddy.

## 6. systemd unit

```bash
# Edit ReadWritePaths / ReadOnlyPaths / UID in the template first.
install -m 0644 /opt/codeagent-mcp/deploy/codeagent-mcp-http.service \
  /etc/systemd/system/codeagent-mcp-http.service
# If you have a writable target root, add it to ReadWritePaths= (example: /opt/myproject).

systemctl daemon-reload
systemctl enable --now codeagent-mcp-http
systemctl status codeagent-mcp-http --no-pager
```

Confirm loopback only:

```bash
ss -ltn | grep 8765
# expect 127.0.0.1:8765 — never 0.0.0.0:8765
```

## 7. Caddy (TLS reverse proxy)

Requires the domain from the starting section to already resolve to this host (otherwise certificate issuance fails).

```bash
cp /opt/codeagent-mcp/deploy/Caddyfile.example /etc/caddy/Caddyfile
# Replace mcp.example.com with CODEAGENT_HOST
systemctl reload caddy   # or enable --now caddy
```

If curl fails with TLS/DNS errors, fix name → IP first; do not expose `:8765` publicly as a workaround.

Checks:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  https://$CODEAGENT_HOST/.well-known/oauth-authorization-server
# expect 200

curl -sS -o /dev/null -w '%{http_code}\n' -X POST "https://$CODEAGENT_HOST/mcp/" \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
# expect 401
```

## 8. GitHub OAuth App

1. Create OAuth App (GitHub → Developer settings).
2. **Authorization callback URL:** `https://<CODEAGENT_HOST>/auth/callback`
3. Put Client ID/secret in `http.env`, restart unit.
4. Complete one browser login; capture stable **`sub`** into `CODEAGENT_ALLOWED_SUBS`; restart.

## 9. Connect a client

Point your MCP client at `https://<CODEAGENT_HOST>/mcp/` and authenticate as an
allowlisted GitHub user, then call `server_info` to confirm the path end to end.

For ChatGPT, the full walkthrough — Developer Mode, connector creation, smoke test, and
troubleshooting — is [`../product/connect-chatgpt.md`](../product/connect-chatgpt.md).
Which other clients work, and what each needs, is in
[`../product/clients.md`](../product/clients.md).

Keep the connector unpublished until you intentionally freeze the tool surface, and prefer
the writable `demo` project for the first write or exec.

## 10. Verification suite on the host

```bash
cd /opt/codeagent-mcp
.venv/bin/python -m pytest -q
bash scripts/threat_tests.sh          # as root; expects public host + unit
bash scripts/regression.sh            # pytest + threat tests + catalog check
```

The threat tests probe the write gate against `TARGET_APP_ROOT` (default
`/srv/example-app`, skipped when it does not exist). Point it at one of your
registered writable checkouts, along with that project's gate variable, to
exercise the check:

```bash
TARGET_APP_ROOT=/srv/myapp TARGET_WRITE_ENV=CODEAGENT_MYAPP_WRITE \
  bash scripts/threat_tests.sh
```

## 11. Day-2 operations

| Action | Command |
|--------|---------|
| Restart app (tmux panes should survive) | `systemctl restart codeagent-mcp-http` |
| Reload proxy | `systemctl reload caddy` |
| Rotate JWT | new `CODEAGENT_JWT_SIGNING_KEY` → restart (forces re-auth) |
| Revoke a human | remove `sub` from `CODEAGENT_ALLOWED_SUBS` → restart |
| Deploy code update | `git pull` as root → `uv sync` if deps change → restart unit; **preserve `.venv/bin` executables** |

## Hard gotchas (from production)

1. **`RestrictSUIDSGID=true` breaks `openat2`** → all `fs_*` fail with errno 38. Leave it off.
2. **`ProtectHome=true` hides `/run/user`** → use `ProtectHome=read-only` + linger + `XDG_RUNTIME_DIR`.
3. **Never publish `:8765`.** Only Caddy on `:443`.
4. Use stable environment-specific project identifiers. Do not rename identifiers to bypass client-side safety controls. Confirm unexpected refusals with host audit (`journalctl -u codeagent-mcp-http`).
5. **Git worktrees** owned differently than `codeagent-mcp` need the MCP git tools' per-invocation `safe.directory` (already in Core) or a normal clone.
6. After `chown root:root` of `/opt/codeagent-mcp`, do **not** `chmod -R a-x` the tree — keep `.venv/bin/*` executable.

## Minimal "stdio only" install (no ChatGPT)

If you only need local MCP clients:

```bash
cd /opt/codeagent-mcp   # or any clone
uv sync
uv run codeagent-mcp    # stdio
```

Skip Caddy/OAuth/systemd. Still install `/etc/codeagent-mcp/projects.yaml` (or set
`CODEAGENT_PROJECTS_FILE`) for any roots you expose — see
[`../product/projects-registry.md`](../product/projects-registry.md).
