# Terminals (tmux)

Persistent PTYs for interactive work. Deterministic non-interactive work stays on `exec_run`.

## Dedicated tmux

- Socket: `CODEAGENT_TMUX_SOCKET` (default `/var/lib/codeagent-mcp/tmux/default.sock`)
- Conf: `exit-empty off`, `exit-unattached off`
- Runtime user: `codeagent-mcp` with `loginctl linger` + `XDG_RUNTIME_DIR`
- Unit: `ProtectHome=read-only` (not `true`) so `ReadWritePaths=/run/user/UID` remains usable for tmux; `ProtectHome=true` made `/run/user` inaccessible and broke `terminal_create`
- MCP systemd unit uses `KillMode=process` so restart does **not** kill the tmux server
- Never uses the human default tmux socket

## Tools

| Tool | Role |
|------|------|
| `terminal_list` | Managed panes for the lease's project |
| `terminal_status` | tmux metadata only (`pane_pid`, command, alive) |
| `terminal_create` | New window + bash + **pipe-pane -O** spool attach |
| `terminal_write` | Literal `send-keys -l` (no implicit Enter) |
| `terminal_key` | Enum: ENTER, CTRL_C, CTRL_D, TAB, ESC, arrows |
| `terminal_interrupt` | TTY Ctrl+C (same as key CTRL_C) |
| `terminal_read` | Incremental spool bytes (cursor / has_more / next_cursor) |
| `terminal_snapshot` | `capture-pane` photo (not incremental) |
| `terminal_close` | kill-pane + drop registry + delete spool |
| `terminal_reset` | close + create same alias (new spool generation) |

## Spool

- Path root: `CODEAGENT_SPOOL_ROOT` (default `/var/lib/codeagent-mcp/spool`)
- Attach on **create** (not lazy on first read)
- Cursor: `v1:<generation>:<logical_offset>` over **raw** bytes
- Response `text` is ANSI/C0-sanitized; `raw_byte_len` reports raw advance
- Rotation replaces the file after detach; cursors below `spool_byte_base` → `CURSOR_EXPIRED`
- Default read cap 100 KiB/call; spool soft max 2 MiB before rotate

## Identity / lease

- Hard ID: tmux `pane_id` (`%N`); soft ID: alias
- Mutators and read/snapshot require matching owner `lease_id` (close/reset may reclaim)
- Max 3 live terminals per lease
- **No silent takeover** of another lease's shell (by design)

## Known limitation

Orphan panes from expired leases still count against the max-3 budget; reclaiming them is explicit today (`terminal_reset` / `terminal_close`). Automatic cleanup of expired-lease terminals is a possible future improvement.
