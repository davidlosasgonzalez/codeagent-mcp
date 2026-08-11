"""Talk to a project's privileged control socket with fixed verbs only.

The operator owns the socket: a systemd ``.socket`` unit created as root and
readable only by the server's group. That inversion is the point — the server
can ask for one named unit to be restarted without holding sudo and without
being able to choose which unit. No user-supplied text ever reaches the helper;
only the three verbs below are sent.
"""

from __future__ import annotations

import re
import socket
from typing import Literal

Verb = Literal["STATUS", "RESTART", "START"]

_ALLOWED: frozenset[str] = frozenset({"STATUS", "RESTART", "START"})
_MAX_REPLY_BYTES = 200_000
_SECRETISH = re.compile(r"(?i)(api[_-]?key|password|secret|token|authorization)\s*[:=]\s*\S+")


class ServiceCtlError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _sanitize(text: str) -> str:
    """Redact anything that looks like a credential before it reaches the client."""
    return _SECRETISH.sub(r"\1=<redacted>", text)


def call_service_ctl(socket_path: str, verb: Verb, *, timeout_s: float = 50.0) -> str:
    """Send a fixed verb to the privileged helper. Rejects unknown verbs."""
    if verb not in _ALLOWED:
        raise ServiceCtlError(f"unsupported verb: {verb!r}")
    chunks: list[bytes] = []
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout_s)
        sock.connect(socket_path)
        sock.sendall(f"{verb}\n".encode())
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
