"""Loopback Streamable HTTP smoke (no public bind, optional no-auth spike)."""

from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from codeagent_mcp.server import build_http_app, create_server


def _inner(app):
    return getattr(app, "app", app)


def _auth_env(monkeypatch) -> None:
    monkeypatch.setenv("CODEAGENT_GITHUB_CLIENT_ID", "dummy-client-id")
    monkeypatch.setenv("CODEAGENT_GITHUB_CLIENT_SECRET", "dummy-client-secret")
    monkeypatch.setenv("CODEAGENT_JWT_SIGNING_KEY", "dummy-jwt-signing-key-for-tests")
    monkeypatch.setenv("CODEAGENT_BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("CODEAGENT_ALLOWED_SUBS", "github|test-sub")


def test_http_app_exposes_mcp_path_without_auth() -> None:
    app = build_http_app(require_auth=False)
    paths = [getattr(r, "path", None) for r in _inner(app).routes]
    assert "/mcp/" in paths


def test_oauth_route_table_when_github_configured(monkeypatch) -> None:
    _auth_env(monkeypatch)
    app = build_http_app(require_auth=True)
    paths = sorted(getattr(r, "path", "") for r in _inner(app).routes)
    assert "/mcp/" in paths
    assert "/auth/callback" in paths
    assert "/.well-known/oauth-authorization-server" in paths
    assert "/.well-known/oauth-protected-resource/mcp/" in paths
    assert "/authorize" in paths
    assert "/token" in paths
    assert "/register" in paths
    assert "/consent" in paths


def test_anonymous_mcp_rejected_when_auth_enabled(monkeypatch) -> None:
    _auth_env(monkeypatch)
    app = build_http_app(require_auth=True)
    with TestClient(app) as client:
        resp = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert resp.status_code in {401, 403}


def test_fail_closed_without_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("CODEAGENT_GITHUB_CLIENT_ID", "dummy-client-id")
    monkeypatch.setenv("CODEAGENT_GITHUB_CLIENT_SECRET", "dummy-client-secret")
    monkeypatch.setenv("CODEAGENT_JWT_SIGNING_KEY", "dummy-jwt-signing-key-for-tests")
    monkeypatch.setenv("CODEAGENT_BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("CODEAGENT_ALLOWED_SUBS", raising=False)
    with pytest.raises(SystemExit, match="CODEAGENT_ALLOWED_SUBS"):
        build_http_app(require_auth=True)


def test_fail_closed_without_jwt_signing_key(monkeypatch) -> None:
    monkeypatch.setenv("CODEAGENT_GITHUB_CLIENT_ID", "dummy-client-id")
    monkeypatch.setenv("CODEAGENT_GITHUB_CLIENT_SECRET", "dummy-client-secret")
    monkeypatch.setenv("CODEAGENT_BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("CODEAGENT_ALLOWED_SUBS", "github|test-sub")
    monkeypatch.delenv("CODEAGENT_JWT_SIGNING_KEY", raising=False)
    with pytest.raises(SystemExit, match="CODEAGENT_JWT_SIGNING_KEY"):
        build_http_app(require_auth=True)


def test_call_server_info_via_fastmcp_inprocess() -> None:
    server = create_server(transport="http")

    async def _call() -> dict:
        result = await server.call_tool("server_info", {})
        data = getattr(result, "data", None)
        if data is not None:
            return data
        structured = getattr(result, "structured_content", None) or getattr(
            result, "structuredContent", None
        )
        if isinstance(structured, dict):
            return structured
        return {"raw": str(result)}

    info = asyncio.run(_call())
    assert info.get("ok") is True or "server_info" in str(info)
