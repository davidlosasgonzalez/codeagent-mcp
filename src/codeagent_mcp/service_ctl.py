"""Talk to a project's privileged control socket with fixed verbs only.

The operator owns the socket: a systemd ``.socket`` unit created as root and
readable only by the server's group. That inversion is the point — the server
can ask for one named unit to be restarted without holding sudo and without
being able to choose which unit.

Verbs are a closed set. Some of them take an argument — which unit to read logs
for, how many lines — so a little caller-influenced text does reach the helper,
and it is confined twice: every token must match ``_SAFE_TOKEN`` here, and the
helper only acts on units the operator already declared for that project. An
argument that names something undeclared comes back as an error, never as
output.
"""

from __future__ import annotations

import re
import socket
from collections.abc import Sequence
from typing import Literal, get_args

Verb = Literal["STATUS", "RESTART", "START", "LOGS", "SNAPSHOT", "RUNAS", "ACTION"]

_ALLOWED: frozenset[str] = frozenset(get_args(Verb))
# Unit names and line counts only: no spaces, no newlines, no shell syntax.
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9@._-]{1,64}$")
_MAX_ARGS = 2
_MAX_REPLY_BYTES = 200_000
_SECRETISH = re.compile(r"(?i)(api[_-]?key|password|secret|token|authorization)\s*[:=]\s*\S+")


class ServiceCtlError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _sanitize(text: str) -> str:
    """Redact anything that looks like a credential before it reaches the client."""
    return _SECRETISH.sub(r"\1=<redacted>", text)


def call_service_ctl(
    socket_path: str,
    verb: Verb,
    *,
    args: Sequence[str] = (),
    timeout_s: float = 50.0,
) -> str:
    """Send a fixed verb, and at most two constrained tokens, to the helper."""
    if verb not in _ALLOWED:
        raise ServiceCtlError(f"unsupported verb: {verb!r}")
    if len(args) > _MAX_ARGS:
        raise ServiceCtlError(f"at most {_MAX_ARGS} arguments are allowed")
    for arg in args:
        if not _SAFE_TOKEN.match(str(arg)):
            raise ServiceCtlError(f"refusing argument {arg!r}")
    line = " ".join([verb, *(str(a) for a in args)])
    chunks: list[bytes] = []
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout_s)
        sock.connect(socket_path)
        sock.sendall(f"{line}\n".encode())
        while True:
            try:
                buf = sock.recv(8192)
            except TimeoutError:
                break
            if not buf:
                break
            chunks.append(buf)
            if sum(len(c) for c in chunks) > _MAX_REPLY_BYTES:
                break
        sock.close()
    except FileNotFoundError as exc:
        raise ServiceCtlError(
            f"control socket missing: {socket_path} (is the .socket unit enabled?)",
            retryable=False,
        ) from exc
    except PermissionError as exc:
        raise ServiceCtlError(
            f"permission denied on control socket {socket_path}",
            retryable=False,
        ) from exc
    except OSError as exc:
        raise ServiceCtlError(f"control socket error: {exc}", retryable=True) from exc
    return _sanitize(b"".join(chunks).decode("utf-8", "replace"))
