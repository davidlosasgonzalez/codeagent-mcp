# Tool surface — CodeAgent MCP

A default install exposes **39 tools**. Source of truth for the names exposed to clients is `list_tools`; the machine-readable snapshot is [`tool-catalog.json`](tool-catalog.json), and `scripts/regression.sh` verifies the live server matches it.

Three more — `service_status`, `service_restart` and `service_start` — appear only when a project in the registry declares a `control_socket`. See [`service-control.md`](service-control.md).

The surface is deliberately frozen: new tools are added only in response to observed friction, because published ChatGPT apps may require recreate/republish after catalog changes.

ChatGPT reaches these tools via **remote MCP over HTTPS/OAuth/FastMCP** (placeholder `https://mcp.example.com/mcp/`). Stdio exposes the same Core tools for local use.

## Workspace

| Tool | Role |
|------|------|
| `workspace_acquire` | Exclusive lease on a configured project root |
| `workspace_status` | Lease state / reclaim |
| `workspace_release` | Release lease (does not kill panes unless asked) |

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
| `browser_ensure` / `browser_set_viewport` / `browser_reload` / `browser_open` / `browser_action` / `browser_snapshot` | Playwright loopback |
| `visual_capture` / `visual_get` / `visual_compare` | ImageContent + artifacts |

## Ops

| Tool | Role |
|------|------|
| `ops_status` | Orphan/lease/terminal hints |
| `ops_cleanup` | Expired artifacts + old spool |

## Meta

| Tool | Role |
|------|------|
| `server_info` | Build/identity probe |

## Annotations (MCP metadata)

Every tool exposes MCP `ToolAnnotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`).

- **UX/metadata only** — clients (e.g. ChatGPT) may use them for risk display. They are **not** a security boundary.
- Enforcement remains lease + path containment + write policy (writable_env gates in `projects.yaml`) + exec/browser gates.
- Profiles: **RO** (pure reads), **MUT** (stateful but not file-destructive by default), **DEST** (`fs_apply_patch`, `fs_write_binary`, `ops_cleanup`), **DEST_OPEN** (`fs_write_file`), **EXEC** (`exec_run`: destructive + openWorld).

Contract test: `tests/test_tool_catalog_contract.py` (stdio ≡ HTTP; schema fingerprints).

## Deliberately out of scope

LSP · semantic index · process manager · test/lint/build wrappers.
