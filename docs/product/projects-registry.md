# Project registry — what the agent may touch

CodeAgent never lets the client invent filesystem roots. **You** declare every
allowed checkout in a server-side YAML file. ChatGPT (or any MCP client) only
passes a **project id**; the server maps that id to a root.

## Quick setup

```bash
# 1. Copy the template
cp /opt/codeagent-mcp/deploy/projects.example.yaml /etc/codeagent-mcp/projects.yaml
chmod 0640 /etc/codeagent-mcp/projects.yaml
chown root:codeagent-mcp /etc/codeagent-mcp/projects.yaml

# 2. Point the service at it (already in deploy/http.env.example)
#    CODEAGENT_PROJECTS_FILE=/etc/codeagent-mcp/projects.yaml

# 3. Edit roots for YOUR apps, then restart
systemctl restart codeagent-mcp-http
```

If `CODEAGENT_PROJECTS_FILE` is unset, the server looks for
`/etc/codeagent-mcp/projects.yaml`, then falls back to
`deploy/projects.example.yaml` in the clone (demo only).

## File format

```yaml
projects:
  - id: demo
    root: /var/lib/codeagent-mcp/demo-root
    writable: true

  - id: myapp-prod
    root: /srv/myapp
    writable_env: CODEAGENT_MYAPP_WRITE   # writable only when env == "1"

  - id: myapp-readonly
    root: /srv/myapp-mirror
    writable: false

  - id: bench
    root: /var/lib/codeagent-mcp/bench
    writable: true
    env:
      MYAPP_DATA_ROOT: /var/lib/codeagent-mcp/bench-data

  - id: myapp-service
    root: /srv/myapp
    writable_env: CODEAGENT_MYAPP_WRITE
    control_socket: /run/myapp-ctl.sock        # lets the agent restart the unit
    health_url: http://127.0.0.1:9000/health   # checked after a restart
    runtime_paths:                             # read-only views of live data
      data: /var/lib/myapp
```

| Field | Required | Meaning |
|-------|----------|---------|
| `id` (or `name`) | yes | Stable id clients pass to `workspace_acquire` / `fs_*` / `exec_run` |
| `root` | yes | Absolute path on **this** host. Clients cannot override it. |
| `writable` | no | Static write gate (`true`/`false`). Default `false`. |
| `writable_env` | no | If set, write is allowed only when that env var equals `1`. Overrides `writable`. |
| `env` | no | Environment variables exported to `exec_run` in this project. |
| `control_socket` | no | Absolute path to a privileged helper socket. Enables the service control tools for this project — see [`service-control.md`](service-control.md). |
| `health_url` | no | Loopback `http`/`https` URL probed after a restart. Refused otherwise. |
| `runtime_paths` | no | Map of name to absolute directory, exposed **read-only** via `runtime_list`/`runtime_read`. Reserved system locations are refused, and the service account still needs POSIX permission — see [`runtime-inspection.md`](runtime-inspection.md). |

### Per-project environment

Use `env` when commands in a project need configuration the client should not be able to
set — a data root, a config path, a test profile. The values are applied after the client's
own `env_overrides`, so server configuration always wins.

Process, loader and credential variables (`PATH`, `LD_*`, `PYTHON*`, `SSH_AUTH_SOCK`,
tokens, and the pinned temp roots) are refused: the registry fails to load with a clear
error rather than exporting them. Clients still cannot name arbitrary variables themselves;
their allowlist is separate and narrower, and can be widened with
`CODEAGENT_EXEC_ENV_PREFIXES` (a comma-separated list of prefixes) if a project genuinely
needs it.

## Security layers (all apply)

1. **Registry** — unknown project id → rejected. No path from the client becomes a root.
2. **Path jail** — tools stay under the registered `root` (`openat2` / PathJail).
3. **Lease** — mutating tools need `workspace_acquire` → `lease_id`.
4. **Write gate** — `writable` / `writable_env` must allow writes (plus OS permissions).
5. **systemd** — add writable roots to `ReadWritePaths=` in the unit; keep secrets in `InaccessiblePaths=` if needed.

### When all five pass and writes still fail

The layer that bites is the one with no error message: **the POSIX ACL mask**.

Adding the service account to a project's group and granting it `rwx` is not enough. If
the directory's mode leaves the group without write, the mask is computed from it and
silently downgrades every group entry:

```
group:codeagent-myapp:rwx    #effective:r-x
mask::r-x
```

The permission is granted and cancelled at once. Nothing logs it; writes just come back
`Permission denied` while the registry, the write gate and `ReadWritePaths=` all look
correct. Check it before re-reading any of them:

```bash
getfacl -p /srv/myapp | grep -E '^mask|effective'
```

To repair a tree whose entries are already granted but masked — and to make new files
inherit both the group and the permission:

```bash
setfacl -R -m g:codeagent-myapp:rwX,g::rwX /srv/myapp
find /srv/myapp -type d -print0 | xargs -0 setfacl -d -m g:codeagent-myapp:rwx,g::rwx
find /srv/myapp -type d -print0 | xargs -0 chmod g+s
```

`rwX` (capital) is deliberate: write everywhere, execute only where it already applied.
User bits are left alone, so Git — which tracks only the owner's execute bit — sees no
mode changes across the tree.

Then prove it rather than assuming it:

```bash
sudo -u codeagent-mcp touch /srv/myapp/.probe && sudo -u codeagent-mcp rm /srv/myapp/.probe
```

## Day-2: add or remove a project

1. Edit `/etc/codeagent-mcp/projects.yaml`.
2. `mkdir` the root (and set group/ACL so `codeagent-mcp` can read/write as intended).
3. If writable: set the env gate in `http.env` and add the path to systemd `ReadWritePaths=`.
4. `systemctl daemon-reload` (if unit changed) && `systemctl restart codeagent-mcp-http`.
5. In ChatGPT: `workspace_acquire(project="myapp-prod")` then use `fs_*` / `exec_run`.

Removing a project: delete its entry, restart, release any leftover leases.

## What clients see

- `workspace_acquire(project="…")` — only ids from your YAML.
- `server_info` — does **not** list host paths.
- Escape attempts (`../`, absolute paths outside root) → `PATH_OUTSIDE_ROOT`.

Related: [`workspace-lease.md`](workspace-lease.md), [`filesystem-readonly.md`](filesystem-readonly.md), [`../architecture/first-install.md`](../architecture/first-install.md).
