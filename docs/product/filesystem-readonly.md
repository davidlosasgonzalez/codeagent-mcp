# Filesystem read-only

Tools: `fs_stat`, `fs_list`, `fs_read`, `fs_search`.

## Rules

- Project roots come from the server registry (`projects.yaml` via `CODEAGENT_PROJECTS_FILE` or `/etc/codeagent-mcp/projects.yaml`). Clients never supply a new root. Guide: [`projects-registry.md`](projects-registry.md).
- Example smoke id in templates: `demo` → `/var/lib/codeagent-mcp/demo-root`.
- Path confinement uses Linux `openat2` with `RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS`.
- Read-only: no writes. Mutating tools need a lease plus the project's write gate (`writable` / `writable_env`).
- `lease_id` is **optional** on these tools (only mutating tools require a lease). If provided, it must be active and is renewed.
- `fs_read` returns `sha256` of the **full file**, a line-range slice, and explicit `truncated`.
- Binaries → `UNSUPPORTED_BINARY` (not returned as text); `sha256` is still included when available.
- `fs_search` uses host `rg` (ripgrep), does not follow symlinks, fail-closed if `rg` missing.

## Limits

- Default read cap 200 KiB (hard 2 MiB).
- Default list 500 entries; search 100 matches.

## Lease binds project

If `lease_id` is provided, the tool binds to **that lease's project** (the `project=` argument is ignored for root resolution). Pass `project=` alone only when intentionally targeting a registered root without holding a lease.
