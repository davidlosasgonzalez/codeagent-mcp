"""Hand a command to the privileged helper to run as the project's account.

``exec_run`` cannot change user itself: the service unit sets
``NoNewPrivileges=true``, which closes sudo and every setuid path. That is worth
keeping, so the crossing happens in the one place that is already privileged and
already audited — the project's control socket.

The command travels in a spool file, not on the socket line. A line protocol
cannot carry an argv containing spaces without inventing a quoting scheme, and
inventing one in front of a privileged exec is how injection gets built by
accident. The line carries only an opaque token.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

DEFAULT_SPOOL_ROOT = Path("/var/lib/codeagent-mcp/runas")
# Long enough that a token cannot be guessed by a local process racing the read.
TOKEN_BYTES = 24
EXIT_MARKER = "__codeagent_exit__="


def spool_root() -> Path:
    return Path(os.environ.get("CODEAGENT_RUNAS_SPOOL", str(DEFAULT_SPOOL_ROOT)))


def write_spec(*, argv: list[str], cwd: str, timeout_s: int) -> str:
    """Persist one command spec and return its token."""
    root = spool_root()
    root.mkdir(parents=True, mode=0o750, exist_ok=True)
    token = secrets.token_urlsafe(TOKEN_BYTES)[:64]
    path = root / f"{token}.json"
    payload = json.dumps({"argv": argv, "cwd": cwd, "timeout_s": int(timeout_s)})
    # 0600 then rename: the helper runs as root and reads it regardless, and no
    # other account gets a window in which to see the command.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.rename(path)
    return token


def discard(token: str) -> None:
    """Drop a spec the helper never consumed (it deletes the ones it reads)."""
    try:
        (spool_root() / f"{token}.json").unlink()
    except OSError:
        pass


def split_exit_code(raw: str) -> tuple[str, int | None]:
    """Separate the helper's exit-code trailer from the command's own output.

    A socket carries bytes, not a process result. Without the trailer the caller
    would have to infer success from output, and reporting "done" off an
    inference is precisely what this tool must not do.
    """
    lines = raw.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index].strip()
        if line.startswith(EXIT_MARKER):
            code = line[len(EXIT_MARKER) :].strip()
            rest = "\n".join(lines[:index]).rstrip("\n")
            try:
                return rest, int(code)
            except ValueError:
                return rest, None
    return raw, None
