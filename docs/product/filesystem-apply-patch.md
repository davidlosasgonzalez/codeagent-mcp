# Filesystem apply patch

Tool: `fs_apply_patch`.

## Contract

- **Requires** `lease_id` (mutating). Effective project is always `lease["project"]`.
- Existing files: `expected_sha256` mandatory (same full-file hash as `fs_read`). Mismatch → `CONFLICT` + reread.
- Structured edit format:
  - `edits`: list of `{old_string, new_string}` (each `old_string` must match exactly once), or
  - `new_content`: full file replace.
- `create=true`: create a new file (empty `expected_sha256`).
- Atomic write: temp file in the **same directory** + `os.replace`, then fsync dir.
- Escape → `PATH_OUTSIDE_ROOT`. EACCES/EROFS → `PERMISSION_DENIED`.
- Write gate: project must allow writes (`writable` / `writable_env` in `projects.yaml`) and the OS/systemd must grant write on that root. The tool does not `chmod` the checkout.

## Output

`sha256`, `created`, `summary` (`lines_before` / `lines_after` / `lines_delta` / `edits_applied`).
