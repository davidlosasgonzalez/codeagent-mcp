"""Mask secret-looking values in text that leaves the server.

Redaction at write time protects new entries; a journal keeps everything a
project logged before it fixed its own logger. This masks on the way out, so
the archive is safe too.

Values are masked, keys are not: a reader can still see that a token was
passed, which is often the fact they need, and a redacted line stays parseable.
"""

from __future__ import annotations

import re

MASK = "[REDACTED]"

# Parameter names whose value is a secret wherever it appears. Whole-word and
# case-insensitive, so "monkey=" is not caught by "key".
_SECRET_KEYS = (
    "k",
    "key",
    "api_key",
    "apikey",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "secret",
    "client_secret",
    "password",
    "passwd",
    "pwd",
    "auth",
    "sig",
    "signature",
    "session",
    "sessionid",
    "code",
)

_KEY_ALT = "|".join(sorted(_SECRET_KEYS, key=len, reverse=True))

# (pattern, replacement). Each keeps the shape it found, so a JSON line stays
# JSON and a query string stays a query string.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # "token": "value"  ->  "token": "[REDACTED]"
    (re.compile(rf'(?i)("(?:{_KEY_ALT})"\s*:\s*)"[^"]*"'), rf'\1"{MASK}"'),
    # Authorization: Bearer xxx  ->  Authorization: [REDACTED]
    # The whole credential goes, scheme included: the scheme alone tells a
    # reader nothing they need and leaving it invited a second mask on top.
    (re.compile(r"(?i)\b((?:proxy-)?authorization)\s*:\s*\S+(?:\s+\S+)?"), rf"\1: {MASK}"),
    # bearer xxx / basic xxx appearing loose in a message
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), rf"\1 {MASK}"),
    # k=value, &token=value
    (re.compile(rf"(?i)\b({_KEY_ALT})=[^&\s\"'<>]+"), rf"\1={MASK}"),
)

# Tokens that announce themselves by prefix, wherever they appear.
_STANDALONE: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{4,}"),
)


def redact(text: str) -> str:
    """Return ``text`` with secret-looking values masked."""
    if not text:
        return text
    out = text
    for pattern, replacement in _RULES:
        out = pattern.sub(replacement, out)
    for pattern in _STANDALONE:
        out = pattern.sub(MASK, out)
    return out


def redaction_count(before: str, after: str) -> int:
    """How many masks the redaction added, for reporting."""
    return after.count(MASK) - before.count(MASK)
