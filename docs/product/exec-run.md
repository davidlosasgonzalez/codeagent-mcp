# exec_run

Deterministic non-interactive command execution for ChatGPT / MCP clients.

## Tool

`exec_run(lease_id, command, cwd?, run_as?, env_overrides?, timeout_s?, max_output_bytes?)`

- `command`: argv list of strings — **never** `shell=True`.
- `lease_id`: required active exclusive lease from `workspace_acquire`.
- `cwd`: defaults to project root; must resolve under that root (`PATH_OUTSIDE_ROOT` otherwise).
- `env_overrides`: deny-by-default allowlist of safe prefixes/keys (locale/CI and common build/test vars). Dangerous keys (`PATH`, `LD_*`, `PYTHONPATH`, secrets) → `RISK_BLOCKED`. Extend the allowlist with `CODEAGENT_EXEC_ENV_PREFIXES`.
- Variables the client must **not** control are configured server-side in the project's `env` map and applied on top of the overrides. See [`projects-registry.md`](projects-registry.md).
- `run_as`: optional; the account this project declares. See below.
- Defaults: `timeout_s=120`, `max_output_bytes=200000` (hard caps 3600s / 2MB).

## Output (`ok: true`)

`stdout`, `stderr`, `exit_code`, `timed_out`, `duration_ms`, `cwd`, `stdout_truncated`, `stderr_truncated`, `signal`, `command`, `lease_id`, `project`.

Timeouts kill the process group (`start_new_session` + `killpg`) and still return structured `ok:true` with `timed_out=true`.

## Running as the project's own account

Some work has to be done by the account that owns the data: a script that opens
a database the service user cannot read, a check that touches credentials whose
owner must not change. Running those as root is not a smaller problem than not
running them — it leaves root-owned files behind and breaks the service that
comes back to them.

Set `run_as` to the account the project declares as `run_as_user`:

```json
{"lease_id": "...", "command": ["./bin/deep-health"], "run_as": "myapp"}
```

**The server cannot do this itself.** Its unit sets `NoNewPrivileges=true`,
which closes `sudo` and every setuid path — deliberately. So the crossing goes
through the project's privileged helper, the same socket the service tools use,
and that helper re-checks both the account and the working directory rather than
trusting the checks made here.

What confines it:

- The account is fixed by the operator's registry entry. A caller naming any
  other account gets `RISK_BLOCKED`, and nothing reaches the socket.
- **`root` is refused by name**, in the registry and again in the helper. The
  point of the field is reaching a service account without reaching root.
- `cwd` confinement is unchanged: outside the project root is `PATH_OUTSIDE_ROOT`.
- A lease is still required.
- The argv travels in a private spool file, not on the socket line. A line
  protocol cannot carry a command containing spaces without inventing a quoting
  scheme, and inventing one in front of a privileged exec is how injection gets
  built by accident.

What it grants, stated plainly: **everything that account can already do.** If it
can read a credential file, so can the agent while `run_as` is set. Declare it
for projects where that is the intent, and leave it out where it is not.

### Output differs from a normal run

The helper merges the two streams onto one socket, so the reply carries `output`
(with `stdout_stderr_merged: true`) instead of separate `stdout`/`stderr`.

`exit_code` is the command's own, read from a trailer the helper appends. If it
comes back `null` the helper did not report one, and **success must not be
assumed** — a socket carries bytes, not a process result.

## Concurrency

At most one in-flight `exec_run` per `lease_id` → `PROCESS_RUNNING` if a second call overlaps.

Activity renews the lease (before and after a successful run start path).

## Write policy

Registered roots are writable only when the project's `writable` / `writable_env` gate allows it and a lease is held. `exec_run` does not change checkout permissions; it runs as the service user under PathJail + OS policy.

## File modes

Commands run with an explicit umask of `022`, not the service umask. Hardened unit files set a restrictive `UMask=` to protect the server's own state, and a child process would inherit it — so a build or test run would leave files in the project tree that only the service account can read, locking out whichever account actually runs the application. The server's private files carry an explicit mode of their own, so nothing depends on inheriting that umask.

Set `CODEAGENT_EXEC_UMASK` (octal) to change it; `027` keeps the tree readable to the project group but not to everyone else. The same value applies to terminal panes, so both tools leave identical modes behind.

## PROCESS_RUNNING with nothing running

The gate that serializes one exec per lease was a bare set of lease ids: no
owner, no clock. When an entry leaked, every later call answered
PROCESS_RUNNING and could name neither what held it nor for how long.

The leak had a cause. On timeout `run_argv` kills the process group, but a
grandchild that called `setsid` is outside it, and while it holds the inherited
stdout the last-resort `communicate()` — which had no timeout — never returns.
That thread is stuck for the life of the process, holding the gate with it.

Three changes:

- The final drain is bounded. If the pipes still will not close, they are
  closed from this side and the reply carries `output_incomplete: true` rather
  than passing off a partial answer as a whole one.
- After the kill, anything left in the child's **session** is killed too, which
  catches descendants that gave themselves a new process group to escape.
- A gate entry whose holding thread no longer exists is stale by definition and
  is reclaimed. The refusal now names the command and its age, and
  `ops_cleanup` sweeps what is left.
