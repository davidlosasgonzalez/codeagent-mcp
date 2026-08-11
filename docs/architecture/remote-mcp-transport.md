# Remote MCP transport for ChatGPT

ChatGPT custom MCP app → MCP Streamable HTTP over HTTPS/TLS → reverse proxy → FastMCP loopback.

**CodeAgent does not expose a separate REST API. The HTTPS endpoint is the transport for the remote MCP server.**

The remote path is:

```
ChatGPT (custom MCP app) → HTTPS :443 → hostname (TLS at reverse proxy)
  → FastMCP Streamable HTTP on loopback → CodeAgent Core
```

Placeholder public URL shape: `https://mcp.example.com/mcp/` (replace with your hostname).

## Version pins

| Package | Version |
|---|---|
| `fastmcp` | **3.4.5** |
| `mcp` (transitive) | **1.29.0** |

## Transports

| Transport | Use | Bind |
|---|---|---|
| `stdio` | local/dev (default CLI) | N/A |
| `http` | remote adapter | **loopback only** (`127.0.0.1` / `::1`); reject `0.0.0.0` |

CLI:

```bash
uv run codeagent-mcp                          # stdio
uv run codeagent-mcp --transport http --no-auth   # loopback testing ONLY
uv run codeagent-mcp --transport http             # requires OAuth env
```

## Routes (FastMCP 3.4.5 + GitHubProvider, `mcp_path=/mcp/`)

Inspected at runtime (not invented):

| Route | Role |
|---|---|
| `/mcp/` | MCP Streamable HTTP endpoint (RequireAuth when OAuth active) |
| `/auth/callback` | IdP GitHub callback (**Authorization callback URL**) |
| `/authorize` | Authorization endpoint (OAuth proxy) |
| `/token` | Token endpoint |
| `/register` | Dynamic Client Registration |
| `/consent` | FastMCP consent UI |
| `/.well-known/oauth-authorization-server` | Authorization Server metadata |
| `/.well-known/oauth-protected-resource/mcp/` | Protected Resource metadata |

**Callback URI formula:** `{CODEAGENT_BASE_URL}/auth/callback`  
Example: `https://mcp.example.com/auth/callback`  
Default `redirect_path`: `/auth/callback` (`CODEAGENT_OAUTH_REDIRECT_PATH` override).

## AuthZ

- Initial provider: GitHub OAuth via `GitHubProvider`.
- Allowlist by stable claim **`sub`** (`CODEAGENT_ALLOWED_SUBS` CSV).
- `login` is bootstrap/human identification only — never authorization.
- Anonymous / `tools/call` without credentials → 401/403 (proven in TestClient).

## Env (HTTP + OAuth)

| Variable | Notes |
|---|---|
| `CODEAGENT_BASE_URL` | Public (or loopback) URL seen by OAuth clients |
| `CODEAGENT_GITHUB_CLIENT_ID` | OAuth App |
| `CODEAGENT_GITHUB_CLIENT_SECRET` | **server only**; never committed or shared |
| `CODEAGENT_ALLOWED_SUBS` | CSV of allowed `sub` (**required** with auth; fail-closed) |
| `CODEAGENT_JWT_SIGNING_KEY` | Persistent FastMCP JWT signing key (**required**) |
| `CODEAGENT_OAUTH_REDIRECT_PATH` | default `/auth/callback` |

## Example deployment shape

Illustrative only — substitute your hostname and registrar:

- **HOST:** `mcp.example.com` (DNS A/AAAA at your-registrar → `203.0.113.1`)
- **Proxy:** Caddy → `127.0.0.1:8765`
- **App:** systemd `codeagent-mcp-http.service` (user `codeagent-mcp`, loopback only)
- **Secrets:** `/etc/codeagent-mcp/http.env` (0640 root:codeagent-mcp)

Expected checks: TLS OK; `GET /.well-known/oauth-authorization-server` → 200; anonymous `POST /mcp/` → **401**; loopback port not reachable publicly.

### Quick rollback

```bash
systemctl stop codeagent-mcp-http caddy
# or remove the host block from the Caddyfile and reload
```

## ChatGPT tool surface refresh

`server_info.capabilities.available_tools` is **metadata inside one tool result**, derived at call time from the live FastMCP tool registry (no parallel hardcoded list). It does **not** register MCP tools with ChatGPT.

ChatGPT binds invocable recipients from the connector's discovered tool catalog. After CodeAgent adds tools, **update/refresh the connector** in ChatGPT Developer Mode, then open a new chat.

Keep the app **unpublished** (or workspace-private) while the tool surface is still growing.

## Hardening

See [`hardening.md`](hardening.md).

## Browser Origin allowlist

ChatGPT OAuth popup sends `Origin: https://chatgpt.com`. `CODEAGENT_ALLOWED_ORIGINS` extends the default list (your public MCP host + chatgpt.com + chat.openai.com + platform.openai.com).
