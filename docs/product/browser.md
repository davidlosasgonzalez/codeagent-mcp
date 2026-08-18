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

## A browser has to be able to end

`browser_ensure` starts one. Until now nothing stopped one: there was no close
tool, releasing the lease did not close it, and no sweep reaped it. A browser
outlived its lease and kept two renderers spinning; on a two-core host three
such trees reached 33 and 71 hours and held about 247% CPU between them, with
0% idle. Measurements taken on that host were worthless and nobody knew why.

There are now three exits, because one is not enough:

| Exit | When |
|------|------|
| `browser_close(lease_id)` | You are done with it |
| `workspace_release` | The lease that opened it goes away |
| `ops_cleanup` | The owning lease is already gone, or nothing owns it |

`browser_close` on a browser that is not running is **success**, not an error.
A caller made to check first will skip the check, and an unclosed browser is
the whole problem.

`ops_cleanup` also kills browser processes launched from our own bundle that
have been reparented to init — whatever started them is gone, so nothing will
ever close them. It reports the ones it is not allowed to signal rather than
counting them as clean: the CPU is still being burned, and someone with the
privilege has to act.

The boundary is the bundle path, and it was checked against a real case: this
host runs unrelated crashpad handlers reparented to init, and they are not
touched.
