"""Structured audit events without secrets or transcripts."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

AUDIT_LOGGER = logging.getLogger("codeagent_mcp.audit")

_SECRET_PATTERNS = (
    re.compile(r"(?i)authorization\s*[:=]\s*\S+"),
    re.compile(r"(?i)bearer\s+[a-z0-9\-._~+/]+=*"),
    re.compile(r"(?i)(client_secret|refresh_token|access_token|jwt_signing_key)\s*[:=]\s*\S+"),
)

_LAYER_HINTS = (
    ("dns", ("name or service not known", "nodename nor servname", "getaddrinfo")),
    ("tls", ("ssl", "certificate", "tls", "handshake")),
    ("proxy", ("502", "503", "504", "bad gateway", "upstream")),
    ("oauth", ("oauth", "unauthorized", "invalid_grant", "allowlist", "not authorized")),
    ("fastmcp", ("fastmcp", "mcp error", "jsonrpc")),
)


def classify_failure(message: str | None) -> str:
    """Best-effort failure layer for ops (observable only; never invent)."""
    text = (message or "").lower()
    for layer, needles in _LAYER_HINTS:
        if any(n in text for n in needles):
            return layer
    return "core"


def redact(text: str) -> str:
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def emit_audit(event: dict[str, Any]) -> None:
    """Emit one JSON audit line. Callers must not pass tokens or full transcripts."""
    payload = {
        "ts": time.time(),
        **event,
    }
    # Defense in depth: scrub string values.
    scrubbed: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            scrubbed[key] = redact(value)[:500]
        else:
            scrubbed[key] = value
    AUDIT_LOGGER.info("%s", json.dumps(scrubbed, sort_keys=True, default=str))


class RedactingFilter(logging.Filter):
    """Drop/redact secret-looking log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        lower = msg.lower()
        if "authorization:" in lower and "bearer" in lower:
            record.msg = "[REDACTED authorization]"
            record.args = ()
            return True
        if any(k in lower for k in ("client_secret=", "jwt_signing_key=", "refresh_token=")):
            record.msg = "[REDACTED secret]"
            record.args = ()
        return True
