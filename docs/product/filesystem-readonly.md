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

## fs_search takes a file as well as a directory

`path` may name a single file; it does not have to be the directory containing
it. The file is confined the same way a directory is, and `rg` is given the
parent as cwd with the file as its one pathspec, so nothing outside the jail is
ever named.

Worth knowing why this took two goes: after the jail accepted a file, searches
still returned **zero matches**. Given one explicit file target, ripgrep drops
the path column from its output, and the match parser read the first field as a
path and discarded every row. The output format is now pinned with
`--with-filename`. An empty result set is the failure mode to distrust — it
looks exactly like a clean answer.

## A character cut in half is not a binary file

`is_binary` read the first 8 KiB and decoded it as UTF-8. Any file whose byte
8192 lands inside a multi-byte character raised `UnicodeDecodeError`, and a
normal Spanish Markdown document came back as `UNSUPPORTED_BINARY` — unreadable
and unpatchable, with `exec_run` as the only way in.

The sample is a prefix, so a partial character at its end is expected rather
than wrong. An incremental decoder holds those bytes back; a genuinely invalid
sequence still fails. Whole files are still checked as whole files, where a
truncated character really is a broken one.
