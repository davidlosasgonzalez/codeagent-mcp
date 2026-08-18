# Service control — run your app's units from the chat

The helper and a commented example live in the repo: install
[`deploy/codeagent-ctl-server`](../../deploy/codeagent-ctl-server) as
`/usr/local/sbin/codeagent-ctl-server`, and copy
[`deploy/ctl.example.conf`](../../deploy/ctl.example.conf) to
`/etc/codeagent-mcp/ctl/<project-id>.conf`. Nothing in either file is specific
to one deployment.

After the agent edits code, someone has to restart the app, read why it did not
come up, and check that it answers. Without this, that someone is you, over SSH,
every time.

These tools let the MCP server operate **the units an operator declared for one
project** while running as an unprivileged account with no `sudo`. They are
optional and appear only for projects that declare a `control_socket`.

## Tools

| Tool | Annotation | Does |
|------|-----------|------|
| `service_status(project, ...)` | read-only | State, journal lines and health probe for the project's units |
| `service_logs(project, unit?, lines?)` | read-only | Recent journal lines, capped at 300 |
| `service_restart(project, unit?)` | destructive | Restarts one unit, then reports status and health |
| `service_start(project, unit?)` | destructive | Starts one unit if inactive |
| `service_action(project, action?)` | destructive | Runs a named action the operator declared, as the project account |
| `http_check(project, path?)` | read-only | Smoke-tests a path on the project's loopback URL |

`unit` selects among the units the operator declared for that project; omit it
for the primary. **A unit that was not declared is refused** — the caller cannot
reach `sshd`, the reverse proxy, the MCP server, the host, or another project's
units, whatever it sends.

## What reaches the helper, and what cannot

Earlier versions of this page said no client text ever reached the helper. That
stopped being true when `unit` and `lines` arrived, so here is the real rule:

- The **verb** is a closed set: `STATUS`, `LOGS`, `RESTART`, `START`, `SNAPSHOT`,
  `RUNAS`. Anything else is rejected before the socket is opened.
- **Arguments** are at most two tokens, each matching `^[A-Za-z0-9@._-]{1,64}$`.
  A space, a newline, a semicolon or a `$(` never survives that, so there is no
  way to append a second command.
- A unit token is then matched against the project's declared list **inside the
  helper**. The server's own check is a courtesy; the helper does not trust it.

Replies are capped at 200 KB and scrubbed of anything shaped like a credential
(`password:`, `token=`, `authorization:`) before reaching the client, because
journal lines are quoted verbatim.

## How the privilege split works

The server never gains the right to run `systemctl`. The operator runs a helper
as root behind a socket only the server's group can open:

```
/run/codeagent-ctl-myapp.sock   root:codeagent-mcp   0660
```

## Setup

The repository ships a generic helper so that adding a project is a
**declaration**, not a copy of two unit files per app.

**1. Declare the project's units** in `/etc/codeagent-mcp/ctl/myapp.conf`:

```bash
UNITS=(myapp.service myapp-worker.service)
PRIMARY=myapp.service

# Optional, per unit: HEALTH_<unit with . and - replaced by _>
HEALTH_myapp=http://127.0.0.1:9000/health
```

**2. Install the socket:**

```bash
codeagent-ctl-install myapp
```

**3. Register the project** in `/etc/codeagent-mcp/projects.yaml`:

```yaml
- id: myapp
  root: /srv/myapp
  writable_env: CODEAGENT_MYAPP_WRITE
  control_socket: /run/codeagent-ctl-myapp.sock
  preview_url: http://127.0.0.1:9000/          # optional, for http_check
  health_url: http://127.0.0.1:9000/health     # optional, probed after restart
```

Both URLs must be `http`/`https` on loopback. The server fetches them and hands
results back, so a non-loopback address would turn the registry into a request
forwarder for whatever the host can reach; the registry refuses to load rather
than allow it.

**4. Restart the server** so the tools register:

```bash
systemctl restart codeagent-mcp-http
```

If the tools are missing entirely, no project in the registry declares a
`control_socket`.

## Named actions

Some work has to run as the account that owns the data — a check that touches
credentials, a script that opens a database the service user cannot read.
`exec_run(run_as=...)` does that, and lets the caller choose the command. A
client may reasonably refuse to send that, since "run this as another user" is
the caller picking a privileged operation.

`service_action` asks for less. The command lives on the host; the caller only
names it:

```bash
# /etc/codeagent-mcp/ctl/myapp.conf
ACTION_health_deep="/opt/myapp/.venv/bin/myapp-health"
ACTION_migrate_check="/opt/myapp/.venv/bin/myapp migrate --check"
ACTIONS_TIMEOUT_S=300
```

Call it with no `action` to list what is declared. A name that is not declared
comes back as an error, never as output, and a name shaped like anything but
`^[a-z][a-z0-9_-]{0,31}$` never reaches the socket at all.

Two things the conf file will bite you on, both measured:

- **Quote values with spaces.** `ACTION_x=/bin/thing --flag` unquoted makes bash
  try to run `--flag` as a command, and the helper dies with a bare 127.
- **Do not prefix other settings with `ACTION_`**, or they list themselves as
  actions. Hence `ACTIONS_TIMEOUT_S`, not `ACTION_TIMEOUT_S`.

### Secrets, without widening a directory

An action may declare an environment file:

```bash
ACTIONENV_health_deep="/etc/example/app.env"
```

systemd reads it **as root** and hands the values to the process, so the target
account never needs read access to the secrets directory. That is a smaller
grant than loosening the directory's permissions, and it leaves nothing new
readable on disk afterwards. The values are never returned to the caller.

### What the conf file is trusted with

`service_action` is a capability, not a sandbox. The name never becomes a
command — it selects one — so what the tool can do is exactly what the operator
wrote down. Trying to decide afterwards whether a declared command is dangerous
would add a guarantee it cannot keep.

What the implementation does hold:

| Invariant | How |
|-----------|-----|
| The caller never supplies a command or an argument | Only the name travels, and it must match `^[a-z][a-z0-9_-]{0,31}$` before anything is sent |
| An action never runs as root | Refused in the MCP by name, and again in the helper before `systemd-run` |
| A declared command is not re-expanded | Word-split as the operator wrote it, with globbing off (`set -f`) so a stray `*` cannot pick up files from the working directory |
| Declared secrets are not readable, only usable | `ACTIONENV_` is read by systemd as root; the values are never returned |
| The conf is not writable by the agent or the project | `root:root 0640` under `/etc`, which the MCP unit cannot write (`ProtectSystem=strict`) |

The part worth saying out loud: **the conf is sourced by root**, so it is root
shell, not data. Its integrity is the boundary. And an action's own output is
returned verbatim — a script that prints a secret publishes it, whatever the
helper does.

For work that genuinely needs root, this tool is the wrong place to put it.
Keeping "can run pre-authorised work, never gets root" as a property you can
state in one line is worth more than the convenience of widening it.

## A restart is not over when systemd says it is

systemd calls a `Type=simple` unit active the moment it forks. Measured on a
FastAPI app: **ten seconds** passed between that and the port accepting
connections, and during those ten seconds `service_restart` reported success
while the app answered nothing.

So a unit that declares a health URL is not considered restarted until that URL
answers. A unit with no HTTP surface of its own — a queue worker, a voice agent
— declares none, and is judged instead by "active, and **still** active five
seconds later", which is what catches a crash-loop.

Do not give a unit the health URL of a different tier. Gating a worker's restart
on the web tier's endpoint produces a green result that means nothing, which is
worse than no gate at all.

So the reply carries two separate answers, because they are two separate
questions:

| Field | Means |
|-------|-------|
| `restart_ok` | The unit came back and stayed up |
| `health_ready` | The project's health endpoint is answering |

`health_ready` is **`null`, not `false`**, when the project declares no health
endpoint: it has not failed a check, it has no check, and reporting `false`
would invent a red light. Pass `wait_for_health_s` to poll instead of guessing —
useful for a worker that is up but still registering, where `restart_ok` is
already true and `health_ready` is not yet.

A health endpoint that answers `503` is reported as `503`, not as "no answer".
The difference between an app replying badly and nothing listening matters, and
the probe used to erase it.

## http_check returns no body

`http_check` reports status, elapsed time, content type and byte count — never
the response body. A page like `/token` answers `200` **by handing out a
credential**, and a smoke test has no business carrying that back into a chat.

For a health body specifically, `service_status` returns the first 200 bytes of
the declared `health_url`. To actually look at a page, use `browser_open`.

## Failure modes

| Error | Means |
|-------|-------|
| `INVALID_ARGUMENT` — no control_socket | The project exists but declares no socket |
| `INVALID_ARGUMENT` — `ERR unit_not_declared` | The unit is not in that project's `UNITS` |
| `INVALID_ARGUMENT` — unit must look like a unit name | The token never reached the socket |
| `SERVICE_CTL_FAILED` — socket missing | The `.socket` unit is not enabled |
| `SERVICE_CTL_FAILED` — permission denied | `SocketGroup` is not the server's group |
| `SERVICE_RESTART_FAILED` | The restart ran but the unit did not come up, or its health URL never answered — read `output` |

## Redaction on read, not only on write

A project that fixes its logger to write `k=[REDACTED]` protects everything it
logs from then on. The journal still holds every request written before the
fix, and `service_logs` was handing those back verbatim.

`service_logs` now masks secret-looking values on the way out and reports how
many masks it added. Values go, keys stay: a reader can still see that a token
was present, which is often the fact they need, and a redacted line stays
parseable — a mask that breaks JSON quoting makes the log useless.

Over-redaction is its own failure, so the match is whole-word and
case-insensitive: `monkey=banana` is not a secret because it ends in `key`.
