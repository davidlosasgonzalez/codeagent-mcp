# Connect CodeAgent MCP to ChatGPT

Step-by-step guide for pointing a ChatGPT custom connector at a deployed CodeAgent MCP
server. This is the reference client: the tool descriptions, limits, and the
`fs_write_file` file-parameter adapter are tuned for it.

ChatGPT's settings UI changes often. Re-check
[OpenAI's own documentation](https://platform.openai.com/docs/mcp) on the day you do this
if a screen does not match what is written here.

## Before you start

1. A CodeAgent MCP server already running behind HTTPS, per
   [`../architecture/first-install.md`](../architecture/first-install.md). ChatGPT cannot
   reach a server on your laptop or a private network: it needs a public HTTPS URL.
2. Your MCP endpoint URL, which is `https://<CODEAGENT_HOST>/mcp/` (note the trailing
   slash).
3. A GitHub account whose stable `sub` claim is listed in `CODEAGENT_ALLOWED_SUBS`.
   Authentication alone does not authorize: an unlisted `sub` is rejected after login.
4. A ChatGPT plan that exposes Developer Mode — Plus, Pro, Business, Enterprise, or Edu,
   on the **web** app.

Verify the server first, from any machine:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  https://<CODEAGENT_HOST>/.well-known/oauth-authorization-server   # expect 200

curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://<CODEAGENT_HOST>/mcp/ \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'   # expect 401
```

A `401` here is success: it proves the endpoint is live and refusing anonymous callers.

## 1. Enable Developer Mode

In ChatGPT on the web: **Settings → Apps → Advanced settings → Developer mode**.

On Business and Enterprise workspaces an admin may have to allow it first, under
**Workspace Settings → Permissions & Roles → Connected Data**.

## 2. Add the connector

Create a custom connector and paste `https://<CODEAGENT_HOST>/mcp/` as the MCP server URL.
Choose **OAuth** as the authentication method — CodeAgent's HTTP transport is fail-closed
and refuses to start without OAuth configured, so no-auth and API-key modes do not apply.

## 3. Authorize

ChatGPT opens a GitHub login popup. Sign in with the account whose `sub` you allowlisted.

If the popup loops or fails, see [Troubleshooting](#troubleshooting) below: the usual cause
is the browser `Origin` not being allowlisted on the server.

## 4. First call

In a new chat, ask the model to call `server_info`. It returns build and identity metadata
and the live tool count, which confirms the whole path end to end: TLS, proxy, OAuth,
allowlist, and FastMCP.

## 5. Smoke test

Use a **writable** project from your `projects.yaml` — the template ships `demo` for
exactly this. Never ask the client to invent a filesystem path; only registered project ids
are valid.

1. `workspace_acquire(project="demo")` → `lease_id`
2. `project_bootstrap`
3. `fs_read` on a known text file
4. A reversible `fs_apply_patch` (or create a file and delete it afterwards)
5. `git_status` / `git_diff`, if that root is a git checkout
6. `exec_run` with a trivial argv, such as `["git","--version"]`
7. Revert your changes so the tree is clean
8. `workspace_release`

No SSH is involved at any point. If the client asks you to approve a write or exec tool,
approve it explicitly.

## Keeping the connector current

After the server gains or renames tools, **refresh the connector** in Developer Mode and
open a new chat. Reconnecting alone can leave a stale tool catalog, and the model will call
tools that no longer match the server. Deleting and recreating the app is not normally
necessary.

## Sharing it with a workspace

On Business and Enterprise plans, a draft connector is private to you; other members cannot
use it until it is published, and only Admins and Owners can publish. A regular member does
not get Developer Mode individually. After publishing, each user authenticates through
GitHub before their first call, so your `CODEAGENT_ALLOWED_SUBS` has to list every one of
them.

Publish only once the tool surface is settled: on Business plans, changing tools or
metadata afterwards may require recreating and republishing the app rather than updating it
in place. There is no documented "unpublish" — you revoke access under **Workspace Settings
→ Apps**.

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| OAuth popup loops, or 403 "Forbidden Origin" | The browser sends `Origin: https://chatgpt.com`. Add it to `CODEAGENT_ALLOWED_ORIGINS` and restart the unit. |
| Login succeeds but every call is rejected | Your `sub` is not in `CODEAGENT_ALLOWED_SUBS`. Capture it from the host audit log after the first login, add it, restart. |
| Service refuses to start | Fail-closed by design: `CODEAGENT_GITHUB_CLIENT_ID`, `_SECRET`, `CODEAGENT_JWT_SIGNING_KEY`, or a non-empty `CODEAGENT_ALLOWED_SUBS` is missing. |
| Model says a tool is unavailable, but the server has it | Stale connector catalog. Refresh the connector, then start a new chat. |
| Write or exec tools are refused with no server-side trace | ChatGPT can refuse destructive tools client-side. Check the host audit log (`journalctl -u codeagent-mcp-http`) before suspecting the server. |
| Writes fail with `WRITE_DISABLED` | The project's `writable` / `writable_env` gate is off, by design on fresh installs. See [`projects-registry.md`](projects-registry.md). |
| Git tools fail on a linked worktree | A worktree owned by another user needs `safe.directory` scoped to the MCP git invocations, or use a normal clone. |
| Everything worked, then stopped after a restart | Browser sessions are not durable across restarts; tmux terminals are. Re-run `browser_ensure`. |
| The model refuses to acquire a project it can see | Short or ambiguous project ids invite client-side refusals. Use stable, environment-specific ids such as `myapp-staging` rather than `app`. |
| An image generated in chat never reaches the server | `fs_write_file` depends on the client filling `openai/fileParams`. It is reliable for attachments; for generated images treat it as best-effort and fall back to `fs_write_binary`. |

## Related

- [`projects-registry.md`](projects-registry.md) — what roots the agent may touch
- [`tool-surface.md`](tool-surface.md) — the 39 tools and their annotations
- [`../architecture/remote-mcp-transport.md`](../architecture/remote-mcp-transport.md) — routes, OAuth, env vars
- [`../architecture/hardening.md`](../architecture/hardening.md) — read before exposing the host
