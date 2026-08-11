"""Remote OAuth wiring (GitHub via FastMCP) and allowlist by stable `sub`."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.middleware import Middleware, MiddlewareContext


class AllowlistSubMiddleware(Middleware):
    """Reject authenticated principals whose JWT `sub` is not allowlisted.

    `login` is never used for authorization — only human bootstrap/identification.
    """

    def __init__(self, allowed_subs: frozenset[str]) -> None:
        self._allowed_subs = allowed_subs

    async def on_call_tool(self, context: MiddlewareContext, call_next: Callable[..., Any]) -> Any:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
        sub = None
        if token is not None:
            claims = getattr(token, "claims", None) or {}
            sub = claims.get("sub")
        if not sub or sub not in self._allowed_subs:
            raise PermissionError("caller sub is not authorized for CodeAgent MCP")
        return await call_next(context)


def parse_allowed_subs(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _normalize_http_url(url: str) -> str:
    """Collapse accidental double-slashes in the path (keep scheme ://)."""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    while "//" in rest:
        rest = rest.replace("//", "/")
    return f"{scheme}://{rest}"


def _patch_fastmcp_cimd_token_audience() -> None:
    """Work around FastMCP 3.4.5 CIMD aud bug: f\"{AnyHttpUrl}/token\" → host//token."""
    from fastmcp.server.auth.auth import PrivateKeyJWTClientAuthenticator

    if getattr(PrivateKeyJWTClientAuthenticator, "_codeagent_aud_fix", False):
        return

    original_init = PrivateKeyJWTClientAuthenticator.__init__

    def __init__(
        self,
        provider: Any,
        cimd_manager: Any,
        token_endpoint_url: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        original_init(
            self,
            provider,
            cimd_manager,
            _normalize_http_url(token_endpoint_url),
            *args,
            **kwargs,
        )

    PrivateKeyJWTClientAuthenticator.__init__ = __init__  # type: ignore[method-assign]
    PrivateKeyJWTClientAuthenticator._codeagent_aud_fix = True  # type: ignore[attr-defined]


def build_github_auth(*, base_url: str) -> GitHubProvider:
    """Build GitHubProvider from env. Secrets must never be logged or committed."""
    _patch_fastmcp_cimd_token_audience()
    client_id = os.environ["CODEAGENT_GITHUB_CLIENT_ID"]
    client_secret = os.environ["CODEAGENT_GITHUB_CLIENT_SECRET"]
    redirect_path = os.environ.get("CODEAGENT_OAUTH_REDIRECT_PATH", "/auth/callback")
    jwt_signing_key = os.environ.get("CODEAGENT_JWT_SIGNING_KEY", "").strip()
    if not jwt_signing_key:
        raise SystemExit(
            "CODEAGENT_JWT_SIGNING_KEY is required for HTTP auth "
            "(persistent signing key; do not rely on ephemeral derivation)"
        )
    normalized = base_url.rstrip("/")
    return GitHubProvider(
        client_id=client_id,
        client_secret=client_secret,
        base_url=normalized,
        issuer_url=normalized,
        redirect_path=redirect_path,
        required_scopes=["read:user"],
        jwt_signing_key=jwt_signing_key,
    )
