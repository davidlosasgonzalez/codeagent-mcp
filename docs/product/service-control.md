# Service control — restart your app from the chat

After the agent edits code, someone has to restart the app. Without this, that
someone is you, over SSH, every time.

These tools let the MCP server restart **one named systemd unit per project**
while running as an unprivileged account with no `sudo`. They are optional and
appear only for projects that declare a `control_socket`.

## Tools

| Tool | Annotation | Does |
|------|-----------|------|
| `service_status(project)` | read-only | Unit state, recent journal lines, health probe |
| `service_restart(project)` | destructive | Restarts the unit, then reports status and health |
| `service_start(project)` | destructive | Starts the unit if inactive |

They cannot restart the MCP server, the reverse proxy or the host, and they
cannot name a unit: the mapping from project to unit lives on the host.

## How the privilege split works

The server never gains the right to run `systemctl`. Instead the operator runs a
tiny helper as root behind a socket that only the server's group can open:

```
/run/myapp-ctl.sock   root:codeagent-mcp   0660
```

The server writes one of three fixed words — `STATUS`, `RESTART`, `START` — and
reads the reply. No client text ever reaches the helper, and an unknown verb is
rejected before the socket is opened. Replies are capped at 200 KB and scrubbed
of anything shaped like a credential (`password:`, `token=`, `authorization:`)
before they reach the client, because journal lines are quoted verbatim.

## Setup

**1. Register the project** in `/etc/codeagent-mcp/projects.yaml`:

```yaml
- id: myapp
  root: /srv/myapp
  writable_env: CODEAGENT_MYAPP_WRITE
  control_socket: /run/myapp-ctl.sock
  health_url: http://127.0.0.1:9000/health   # optional
```

`health_url` must be `http`/`https` on loopback. The server fetches it and hands
the first 200 bytes of the body back to the client, so a non-loopback address
would turn the registry into a request forwarder for whatever the host can
reach; the registry refuses to load rather than allow it.

**2. Add the helper.** A socket unit and a service that reads one line:

```ini
# /etc/systemd/system/myapp-ctl.socket
[Socket]
ListenStream=/run/myapp-ctl.sock
SocketUser=root
SocketGroup=codeagent-mcp
SocketMode=0660
Accept=yes

[Install]
WantedBy=sockets.target
```

```ini
# /etc/systemd/system/myapp-ctl@.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/myapp-ctl
StandardInput=socket
StandardOutput=socket
```

The helper itself maps the three verbs onto one hardcoded unit — never onto its
input:

```bash
#!/usr/bin/env bash
set -euo pipefail
UNIT=myapp.service
read -r verb || exit 1
case "$verb" in
  STATUS)  systemctl show "$UNIT" -p ActiveState -p SubState; journalctl -u "$UNIT" -n 40 --no-pager ;;
  RESTART) systemctl restart "$UNIT" && echo restart_ok; systemctl show "$UNIT" -p ActiveState ;;
  START)   systemctl start "$UNIT" && echo start_ok; systemctl show "$UNIT" -p ActiveState ;;
  *)       echo "refused: $verb"; exit 2 ;;
esac
```

**3. Enable and restart:**

```bash
systemctl enable --now myapp-ctl.socket
systemctl restart codeagent-mcp-http
```

`service_status("myapp")` should now answer. If the tools are missing entirely,
the registry has no project with a `control_socket`.

## Failure modes

| Error | Means |
|-------|-------|
| `INVALID_ARGUMENT` — no control_socket | The project exists but declares no socket |
| `SERVICE_CTL_FAILED` — socket missing | The `.socket` unit is not enabled |
| `SERVICE_CTL_FAILED` — permission denied | `SocketGroup` is not the server's group |
| `SERVICE_RESTART_FAILED` | The restart ran but the unit did not come up — read `output` |
