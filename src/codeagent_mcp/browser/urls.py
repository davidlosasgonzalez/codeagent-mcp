"""Loopback-only URL allowlist."""

from __future__ import annotations

from urllib.parse import urlparse

ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ALLOWED_SCHEMES = frozenset({"http", "https"})


def validate_navigation_url(url: str) -> str:
    """Return normalized URL or raise ValueError with reason."""
    if not url or not isinstance(url, str):
        raise ValueError("url is required")
    raw = url.strip()
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(f"scheme {parsed.scheme!r} not allowed (loopback http/https only)")
    if parsed.username or parsed.password:
        raise ValueError("URLs with credentials are not allowed")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ValueError(f"host {host!r} not allowed (loopback only)")
    # Reject oddities like file smuggled via path
    if parsed.path.startswith("//") and not host:
        raise ValueError("invalid URL")
    return raw
