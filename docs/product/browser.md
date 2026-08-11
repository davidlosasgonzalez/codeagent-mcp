# Browser bridge (Playwright)

Headless Chromium for loopback UI interaction. **Not** for open Internet. Screenshots → [`visual.md`](visual.md).

## Hard constraints

- Tests use an in-process fixture HTTP server; they do not require mutating a registered app root.
- Chromium is **not** durable across MCP restart (unlike tmux). `KillMode=process` means orphans must be reaped via `browser_ensure` / process exit.
- Max one browser session; lease ownership required.
- Authenticated browser sessions are lost on MCP restart (expected). Prefer ephemeral test users/DB for automation. Encrypted `storage_state` with TTL is deliberately not implemented.

## Tools

| Tool | Role |
|------|------|
| `browser_ensure` | Launch/reuse Chromium; optional `width`/`height` |
| `browser_set_viewport` | Arbitrary viewport (max 3840×2160) |
| `browser_reload` | Reload; `ignore_cache=true` for hard reload |
| `browser_open` | Navigate allowlisted loopback URL |
| `browser_action` | click / fill / press / select / wait |
| `browser_snapshot` | DOM highlights + capped a11y + console/errors |

## Allowlist

`http(s)://127.0.0.1`, `localhost`, `::1` only. `file://` and external hosts → `RISK_BLOCKED`. Final URL re-checked after navigation.

## Paths

- Browsers: `PLAYWRIGHT_BROWSERS_PATH` → `/var/lib/codeagent-mcp/playwright`
- Profile: `CODEAGENT_BROWSER_PROFILE` → `/var/lib/codeagent-mcp/browser-profile`

## Smoke tip

Use a registered smoke project such as `demo` under `/var/lib/codeagent-mcp/demo-root`. Avoid `file:///etc/passwd` in smoke prompts.
