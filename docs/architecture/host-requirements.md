# Host requirements

Generic requirements for running CodeAgent MCP on a dedicated Linux host. No production hostnames or IPs are documented here.

For the ordered greenfield checklist, see [`first-install.md`](first-install.md). For the remote ChatGPT path, see [`remote-mcp-transport.md`](remote-mcp-transport.md). Project registry: [`../product/projects-registry.md`](../product/projects-registry.md).

## Operating system

**Linux only, on x86-64 or arm64.** This is a hard requirement, not a preference:

- Path confinement calls [`openat2(2)`](https://man7.org/linux/man-pages/man2/openat2.2.html)
  directly, by syscall number, with no portable fallback. That syscall arrived in **Linux
  5.6**, so older kernels cannot run the filesystem tools at all. Ubuntu 20.04's stock 5.4
  kernel and other pre-5.6 kernels are out.
- The rest of the stack is Linux-specific too: systemd for the service unit, `ss` for the
  bind checks, `setfacl`/`getfacl` for durable group-write policy on project roots.
- **macOS and Windows are not supported**, including for development: the `fs_*`,
  `git_*`, browser, and project-intelligence tests all fail without `openat2`.

Check the kernel version, then confirm the syscall actually answers — some distributions
backport it, and hardening profiles can take it away again:

```bash
uname -srm    # expect 5.6 or newer, x86_64 or aarch64

python3 - <<'PY'
import ctypes, ctypes.util, errno, os
libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
class How(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64), ("resolve", ctypes.c_uint64)]
fd = os.open("/tmp", os.O_RDONLY | os.O_DIRECTORY)
how = How(flags=os.O_RDONLY, mode=0, resolve=0x08 | 0x02)   # RESOLVE_BENEATH | NO_MAGICLINKS
ctypes.set_errno(0)
ok = libc.syscall(437, fd, b".", ctypes.byref(how), ctypes.sizeof(how)) >= 0
print("openat2: available" if ok else f"openat2: MISSING (errno={errno.errorcode.get(ctypes.get_errno())})")
PY
```

An `ENOSYS` here after a working install usually means a systemd directive removed the
syscall — see the `RestrictSUIDSGID` warning in [`hardening.md`](hardening.md).

Standard userland beyond that: `bash`, `git`, `tmux`, `ripgrep`, and the ACL tools.

## Runtime

- **Python 3.12** (pin via `.python-version`).
- **`uv`** for install/sync (version-locked with `uv.lock`).
- Package lives outside target project trees (e.g. `/opt/codeagent-mcp`).

## Service model

- Restricted Unix identity for the MCP process (no sudo, no Docker socket).
- **systemd** user or system unit for the FastMCP HTTP service (loopback only).
- Dedicated tmux socket; `XDG_RUNTIME_DIR` (typically `/run/user/UID`) must be reachable from the unit (`ProtectHome=read-only` rather than `true` when linger/runtime dirs are required).

## Remote ChatGPT path (optional for stdio-only)

- Public hostname + TLS (Let's Encrypt or equivalent).
- **Caddy** (or another reverse proxy) terminating HTTPS on `:443` and proxying to FastMCP on loopback (e.g. `127.0.0.1:8765`). Template: `deploy/Caddyfile.example`.
- GitHub OAuth App + allowlist by stable `sub`.
- DNS at your registrar pointing the hostname at the host.

Stdio-only local/dev clients do **not** require a domain, Caddy, or public `:443`.

## Project roots

- Configured in `projects.yaml` via `CODEAGENT_PROJECTS_FILE` or `/etc/codeagent-mcp/projects.yaml` (fallback: `deploy/projects.example.yaml` in the clone).
- Template smoke project: **`demo`** → `/var/lib/codeagent-mcp/demo-root` (`writable: true`). Add your own apps with absolute `root` paths; clients never invent roots.
- Mutating tools require an exclusive workspace lease; write enablement is controlled by **`writable` / `writable_env`** per project, plus systemd `ReadWritePaths=` for writable roots.

## Verification

- `uv run pytest` (and project regression scripts when present).
- Threat/smoke scripts under `scripts/` after install.
- Confirm loopback MCP is not reachable from the public internet.
