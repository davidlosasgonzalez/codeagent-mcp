# Runtime inspection (optional)

Read-only access to the data a deployed service actually runs on.

## The gap this closes

A checkout tells you what the code says. It never tells you what the running service
holds. So "the migration landed", "the reseed took", "production has the new units" were
claims nobody on this surface could check — the only ways to look were credentials the
agent must not have, or a filesystem escape the jail exists to prevent.

The result was worse than a missing feature: work got reported as done in production on
the strength of the code being correct in the checkout.

`runtime_list` and `runtime_read` give the narrow version instead. The operator names
directories; callers may list and read under exactly those, and nothing else.

## Enabling it

Add `runtime_paths` to a project in `projects.yaml` — a map of short name to absolute
directory:

```yaml
projects:
  - id: myapp-prod
    root: /srv/myapp
    writable_env: CODEAGENT_MYAPP_WRITE
    runtime_paths:
      data: /var/lib/myapp
      state: /var/lib/myapp-state
```

Names match `^[a-z][a-z0-9_-]{0,31}$`. Paths must be absolute, and `/` and anything
under `/etc`, `/root`, `/proc`, `/sys`, `/dev` or `/boot` are refused outright.

If no project declares `runtime_paths`, **the tools are not registered at all** — they do
not appear in `tools/list`.

## Two halves, and they are separate

Declaring a path **authorizes** it. It does not **grant access to it**.

The server still runs as its own unprivileged account, so reading `/var/lib/myapp` also
needs POSIX permission — a group the service account belongs to, or an ACL. A hardened
unit additionally needs the path in `ReadOnlyPaths=`. That separation is deliberate: an
operator editing one YAML file cannot accidentally hand out the host, and an agent
cannot widen its own reach by naming a path.

## Tools

| Tool | Role |
|------|------|
| `runtime_list(project, name?, path?, max_entries?, lease_id?)` | With no `name`: the declared views. With `name`: a directory listing inside it |
| `runtime_read(name, path, project?, max_bytes?, lease_id?)` | Byte-capped file read inside one view |

Both are read-only. There is no runtime write, no runtime exec, and no way to reach the
project checkout through them — a view is its own jail, resolved with the same `openat2`
confinement as `fs_read`.

`lease_id` is optional and binds the call to the leased project when set, like the other
read-only tools.

## What it is not

It is not a database client. Reading a SQLite file's bytes is not querying it — if you
need to *ask* the running service something, that belongs behind the privileged control
socket (see [`service-control.md`](service-control.md)), where the operator writes the
verb and its answer is fixed and auditable.
