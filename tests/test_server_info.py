"""Tests for server_info and server wiring."""

from __future__ import annotations

import asyncio
from typing import Any

from codeagent_mcp import __version__
from codeagent_mcp.server import create_server
from codeagent_mcp.tools.server_info import build_server_info


def _unwrap_tool_result(result: Any) -> dict[str, Any]:
    info = getattr(result, "structured_content", None)
    assert isinstance(info, dict)
    return info


def test_build_server_info_has_version_and_no_secrets() -> None:
    info = build_server_info(
        transport="stdio",
        available_tools=["server_info", "workspace_acquire"],
    )
    assert info["ok"] is True
    assert info["version"] == __version__
    assert info["name"] == "codeagent-mcp"
    assert info["transport"] == "stdio"
    assert "server_info" in info["capabilities"]["available_tools"]
    assert "workspace_acquire" in info["capabilities"]["available_tools"]
    blob = str(info).lower()
    for forbidden in ("password", "token", "secret", "/root/", "ssh"):
        assert forbidden not in blob


def test_create_server_registers_server_info() -> None:
    server = create_server(transport="stdio")

    async def _list() -> list[str]:
        tools = await server.list_tools()
        return [t.name for t in tools]

    names = asyncio.run(_list())
    assert "server_info" in names
    assert "workspace_acquire" in names
    assert "workspace_status" in names
    assert "workspace_release" in names


def test_server_info_available_tools_match_list_tools() -> None:
    """server_info must not drift from the live MCP catalog."""

    async def _check() -> None:
        server = create_server(transport="stdio")
        tools = await server.list_tools()
        live = {t.name for t in tools}
        info = _unwrap_tool_result(await server.call_tool("server_info", {}))
        advertised = set(info["capabilities"]["available_tools"])
        assert advertised == live
        assert "browser_set_viewport" in advertised
        assert "browser_reload" in advertised

    asyncio.run(_check())


def test_server_info_includes_registered_browser_viewport_tools() -> None:
    """Regression: registered browser_* must appear in server_info capabilities."""

    async def _check() -> None:
        server = create_server(transport="stdio")
        info = _unwrap_tool_result(await server.call_tool("server_info", {}))
        tools = info["capabilities"]["available_tools"]
        assert "browser_set_viewport" in tools
        assert "browser_reload" in tools

    asyncio.run(_check())
