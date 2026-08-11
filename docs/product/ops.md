# Observability, quotas, and recovery

## Audit

Logger `codeagent_mcp.audit` emits **one JSON line** per event:

| Field | Notes |
|-------|--------|
| `ts` | epoch |
| `event` | `tool_call`, `cleanup_spool`, `orphan_detect`, … |
| `tool` / `lease_id` / `pane_id` | opaque ids |
| `sub` / `login` | stable identity; **never** bearer tokens |
| `authz` | `allow` / `deny` |
| `error_code` / `layer` | `dns\|tls\|proxy\|oauth\|fastmcp\|core` |
| `ok` | bool |

`RedactingFilter` on handlers scrubs `Authorization` / `client_secret` / tokens.

**Do not** log transcripts or full `exec_run` stdout.

## Quotas / TTL

| Resource | Control |
|----------|---------|
| Artifacts | TTL + global quota + **per-lease** quota (`CODEAGENT_ARTIFACT_LEASE_QUOTA`) |
| Spool | per-pane size + cleanup TTL by mtime (`CODEAGENT_SPOOL_TTL_S`, default 86400) |
| HTTP `/mcp/` | in-process rate limit (`CODEAGENT_HTTP_RATE_LIMIT` / `WINDOW_S`) + Caddy body/timeouts |

## Tools

- `ops_status` — lease↔terminal orphan hints
- `ops_cleanup` — expired artifacts + aged spool + detection

## Recovery

1. `systemctl restart codeagent-mcp-http` — tmux survives (`KillMode=process`); OAuth state under `~/.local/share/fastmcp`.
2. `systemctl reload caddy` — does not touch auth.
3. Anonymous after restart → still **401** (covered by `scripts/threat_tests.sh`).
4. JWT rotation: see [`../architecture/hardening.md`](../architecture/hardening.md).

Browser sessions are **not** durable across MCP restart.

## Ops note — lease store ownership

Admin edits to `/var/lib/codeagent-mcp/leases.json` must finish with `chown codeagent-mcp:codeagent-mcp` + mode `600`. A root-owned file surfaces as `INTERNAL_ERROR: lease store unreadable`.
