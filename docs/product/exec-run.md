# exec_run

Deterministic non-interactive command execution for ChatGPT / MCP clients.

## Tool

`exec_run(lease_id, command, cwd?, env_overrides?, timeout_s?, max_output_bytes?)`

- `command`: argv list of strings — **never** `shell=True`.
- `lease_id`: required active exclusive lease from `workspace_acquire`.
- `cwd`: defaults to project root; must resolve under that root (`PATH_OUTSIDE_ROOT` otherwise).
- `env_overrides`: deny-by-default allowlist of safe prefixes/keys (locale/CI and common build/test vars). Dangerous keys (`PATH`, `LD_*`, `PYTHONPATH`, secrets) → `RISK_BLOCKED`. Extend the allowlist with `CODEAGENT_EXEC_ENV_PREFIXES`.
- Variables the client must **not** control are configured server-side in the project's `env` map and applied on top of the overrides. See [`projects-registry.md`](projects-registry.md).
- Defaults: `timeout_s=120`, `max_output_bytes=200000` (hard caps 3600s / 2MB).

## Output (`ok: true`)

`stdout`, `stderr`, `exit_code`, `timed_out`, `duration_ms`, `cwd`, `stdout_truncated`, `stderr_truncated`, `signal`, `command`, `lease_id`, `project`.

Timeouts kill the process group (`start_new_session` + `killpg`) and still return structured `ok:true` with `timed_out=true`.

## Concurrency

At most one in-flight `exec_run` per `lease_id` → `PROCESS_RUNNING` if a second call overlaps.

Activity renews the lease (before and after a successful run start path).

## Write policy

Registered roots are writable only when the project's `writable` / `writable_env` gate allows it and a lease is held. `exec_run` does not change checkout permissions; it runs as the service user under PathJail + OS policy.

## File modes

Commands run with an explicit umask of `022`, not the service umask. Hardened unit files set a restrictive `UMask=` to protect the server's own state, and a child process would inherit it — so a build or test run would leave files in the project tree that only the service account can read, locking out whichever account actually runs the application. The server's private files carry an explicit mode of their own, so nothing depends on inheriting that umask.

Set `CODEAGENT_EXEC_UMASK` (octal) to change it; `027` keeps the tree readable to the project group but not to everyone else. The same value applies to terminal panes, so both tools leave identical modes behind.
