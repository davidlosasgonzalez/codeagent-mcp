"""Binary-vs-text detection for fs_read."""

from __future__ import annotations

_SAMPLE = 8192


def is_binary(data: bytes) -> bool:
    """Return True if sample looks binary (NUL or undecodable as UTF-8)."""
    sample = data[:_SAMPLE]
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False
