# Tool surface — CodeAgent MCP

A default install exposes **43 tools**. Source of truth for the names exposed to clients is `list_tools`; the machine-readable snapshot is [`tool-catalog.json`](tool-catalog.json), and `scripts/regression.sh` verifies the live server matches it.

Six more — `service_status`, `service_logs`, `service_restart`, `service_start`, `service_action` and `http_check` — appear only when a project in the registry declares a `control_socket`. See [`service-control.md`](service-control.md).

Three more — `runtime_list`, `runtime_read` and `runtime_tail` — appear only when a project declares `runtime_paths`. See [`runtime-inspection.md`](runtime-inspection.md).

The surface is deliberately frozen: new tools are added only in response to observed friction, because published ChatGPT apps may require recreate/republish after catalog changes.

ChatGPT reaches these tools via **remote MCP over HTTPS/OAuth/FastMCP** (placeholder `https://mcp.example.com/mcp/`). Stdio exposes the same Core tools for local use.

## Workspace

| Tool | Role |
|------|------|
| `workspace_acquire` | Exclusive lease on a configured project root |
| `workspace_status` | Lease state / reclaim |
| `workspace_release` | Release lease (does not kill panes unless asked) |
| `workspace_diff_since_acquire` | Changes made **since this lease was acquired**, excluding dirt that was already in the checkout |

Projects: configured in `projects.yaml` (example: production roots under `/srv/…` writable under lease + `writable_env` gates; `demo` for smoke). Use stable environment-specific project identifiers.

## Filesystem

| Tool | Role |
|------|------|
| `fs_stat` | Metadata |
| `fs_list` | Directory listing |
| `fs_read` | Range read + sha256 (binaries → `UNSUPPORTED_BINARY` still includes sha256) |
| `fs_search` | ripgrep search |
| `fs_apply_patch` | Structured text edit with `expected_sha256` (lease + write policy) |
| `fs_write_binary` | Binary write from Base64 (`content_base64`, max 2_000_000 decoded bytes; lease + write policy) |
| `fs_write_file` | ChatGPT `openai/fileParams` → allowlisted HTTPS download → same Core write gates (`openWorldHint=true`) |

## Project intelligence

| Tool | Role |
|------|------|
| `project_bootstrap` | Compact orientation + `recommended_reads` |
| `project_instructions` | Scoped AGENTS/CLAUDE/rules |
| `project_skills_list` | Skill manifests only |
| `project_skill_read` | One skill body (no `!command` exec) |
| `project_agents_list` | Subagent definition manifests |
| `project_agent_read` | One subagent contract, to adopt yourself (spawns nothing) |

There is deliberately no `project_skill_run` tool.

## Git (read-only)

| Tool | Role |
|------|------|
| `git_status` | Branch + staged/unstaged/untracked (capped) |
| `git_diff` | Summary + byte-capped unified diff (`staged`/`unstaged`/`both`) |

No commit/push/checkout wrappers. Use `exec_run` for uncovered Git.

## Exec

| Tool | Role |
|------|------|
| `exec_run` | Argv + lease + timeout/killpg + output caps |

## Terminal (tmux)

| Tool | Role |
|------|------|
| `terminal_list` / `terminal_status` / `terminal_create` | Lifecycle |
| `terminal_write` / `terminal_key` | Input |
| `terminal_read` / `terminal_snapshot` | Spool / capture-pane |
| `terminal_interrupt` / `terminal_close` / `terminal_reset` | Control |

## Browser / visual

| Tool | Role |
|------|------|
| `browser_ensure` / `browser_set_viewport` / `browser_reload` / `browser_open` / `browser_action` / `browser_snapshot` / `browser_close` | Playwright loopback; browser_close frees its processes |
| `visual_capture` / `visual_get` / `visual_compare` | ImageContent + artifacts |

## Ops

| Tool | Role |
|------|------|
| `ops_status` | Orphan/lease/terminal hints |
| `ops_cleanup` | Expired artifacts + old spool |

## Meta

| Tool | Role |
|------|------|
| `server_info` | Build identity, and whether your tool list is current |

`server_info` answers two questions a client cannot answer from the tool list
alone.

**Which build is this?** `version` is the semver; `build` names the deployed
code — `commit`, `dirty`, `deployed_at`. `dirty` matters more than it looks: a
host redeployed from a working tree runs code no commit describes, and a commit
sha on its own would name something else. With no stamp installed, the fields
are `null` and `source` is `unstamped` — never a guess.

The stamp lives at `/etc/codeagent-mcp/build.json`, root-owned and outside the
checkout, so the service can read its identity and cannot write it. It is read
once at process start: re-reading per call would report a stamp a later deploy
wrote and this process never loaded.

**Is my tool list current?** `capabilities.tool_surface` carries a `count` and a
`fingerprint` over the tool names and their input properties. A client that
compares them against its own view learns its catalogue is stale, instead of
concluding a capability does not exist:

> A cached `service_restart` schema without `wait_for_health_s` looks exactly
> like a server that never had it. That cost a round trip, and the fingerprint
> is the cheapest way to tell the two apart.

Descriptions are excluded from the digest on purpose — a wording fix should not
look like a capability change. Both numbers depend on the registry, since the
gated tool groups above only appear for projects that declare them.

## A stale catalogue is a real failure mode

A client reported `service_restart` with `project` and `unit` but no
`wait_for_health_s`, then re-discovered that one tool and saw all three. The
implementation was never the problem, and it is worth recording where the
problem is not:

| Checked | Result |
|---------|--------|
| In-process `list_tools` | Full schema, 51 tools |
| `tools/list` over the Streamable HTTP transport, middleware active | Identical, one page, no `nextCursor` |
| Middleware | None implements `on_list_tools`; only `on_call_tool` |
| Caddy | Plain `reverse_proxy`, no cache module |

The shape that arrived is itself the evidence: `unit` present and
`wait_for_health_s` absent describes this server exactly as it was between two
deploys on 18/08/2026, and at no other time. That is a faithful copy of an
older catalogue, not a corrupted one — so the copy is being kept somewhere
upstream of this host.

Three things help, none of which can reach that cache directly:

- `/mcp/*` is served `Cache-Control: no-store` and `Vary: Authorization`, so no
  HTTP-level intermediary may keep one.
- The server `instructions`, delivered on `initialize`, tell a client to compare
  `capabilities.tool_surface` against the tools it holds and to re-discover a
  specific tool before deciding an argument does not exist.
- **Tool results name the arguments they expect.** The reply that says *"pass
  `wait_for_health_s` to poll instead of guessing"* is what made the client look
  again, and it worked. A capability that only exists in a schema is one cache
  away from invisible; one that a result mentions repairs the client's view for
  free.

After a deploy that changes the surface, refreshing the connector remains the
reliable fix.

## Annotations (MCP metadata)

Every tool exposes MCP `ToolAnnotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`).

- **UX/metadata only** — clients (e.g. ChatGPT) may use them for risk display. They are **not** a security boundary.
- Enforcement remains lease + path containment + write policy (writable_env gates in `projects.yaml`) + exec/browser gates.
- Profiles: **RO** (pure reads), **MUT** (stateful but not file-destructive by default), **DEST** (`fs_apply_patch`, `fs_write_binary`, `ops_cleanup`), **DEST_OPEN** (`fs_write_file`), **EXEC** (`exec_run`: destructive + openWorld).

Contract test: `tests/test_tool_catalog_contract.py` (stdio ≡ HTTP; schema fingerprints).

## Deliberately out of scope

LSP · semantic index · process manager · test/lint/build wrappers.
