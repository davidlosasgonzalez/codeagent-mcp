"""Build identity, and telling a stale catalogue from a missing capability.

Both come from the same complaint: a client could not answer "which build am I
talking to, and is what I know about it current?". server_info said 0.1.0
before and after every redeploy, and a cached service_restart schema without
wait_for_health_s was indistinguishable from a server that did not have it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from codeagent_mcp import __version__
from codeagent_mcp.build_info import read_build_stamp
from codeagent_mcp.server import create_server
from codeagent_mcp.tools.server_info import build_server_info, tool_fingerprint


def _server_info() -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        server = create_server(transport="stdio")
        result = await server.call_tool("server_info", {})
        assert result.structured_content is not None
        return result.structured_content

    return asyncio.run(_run())


# --- the stamp -------------------------------------------------------------


def test_a_stamp_is_reported_as_written(tmp_path: Path) -> None:
    stamp = tmp_path / "build.json"
    stamp.write_text(
        json.dumps(
            {
                "commit": "f727ae6",
                "dirty": True,
                "deployed_at": "2026-08-18T17:00:00+02:00",
                "note": "tests y docs",
            }
        ),
        encoding="utf-8",
    )
    out = read_build_stamp(stamp)
    assert out["commit"] == "f727ae6"
    assert out["dirty"] is True
    assert out["source"] == "stamp"


def test_no_stamp_is_unstamped_never_a_guess(tmp_path: Path) -> None:
    out = read_build_stamp(tmp_path / "absent.json")
    assert out["source"] == "unstamped"
    assert out["commit"] is None
    assert out["dirty"] is None


@pytest.mark.parametrize("body", ["not json at all", "[]", '"a string"', ""])
def test_a_broken_stamp_does_not_break_server_info(tmp_path: Path, body: str) -> None:
    """server_info must survive a host that was never stamped correctly."""
    stamp = tmp_path / "build.json"
    stamp.write_text(body, encoding="utf-8")
    assert read_build_stamp(stamp)["source"] == "unstamped"


def test_a_stamp_cannot_smuggle_extra_fields(tmp_path: Path) -> None:
    stamp = tmp_path / "build.json"
    stamp.write_text(
        json.dumps({"commit": "abc1234", "token": "shhh", "path": "/root/x"}),
        encoding="utf-8",
    )
    out = read_build_stamp(stamp)
    assert out["commit"] == "abc1234"
    assert "token" not in out
    assert "path" not in out


def test_dirty_is_none_when_unknown_not_false(tmp_path: Path) -> None:
    """A stamp that does not say is not a stamp that says clean."""
    stamp = tmp_path / "build.json"
    stamp.write_text(json.dumps({"commit": "abc1234"}), encoding="utf-8")
    assert read_build_stamp(stamp)["dirty"] is None


def test_server_info_carries_the_build_block() -> None:
    info = _server_info()
    assert info["version"] == __version__
    assert set(info["build"]) == {"commit", "dirty", "deployed_at", "note", "source"}


# --- the fingerprint -------------------------------------------------------


def test_the_fingerprint_is_order_independent() -> None:
    a = [{"name": "b", "properties": ["y", "x"]}, {"name": "a", "properties": ["p"]}]
    b = [{"name": "a", "properties": ["p"]}, {"name": "b", "properties": ["x", "y"]}]
    assert tool_fingerprint(a) == tool_fingerprint(b)


def test_a_new_argument_changes_the_fingerprint() -> None:
    """This is the whole point: wait_for_health_s appearing must be visible."""
    before = [{"name": "service_restart", "properties": ["project", "unit"]}]
    after = [{"name": "service_restart", "properties": ["project", "unit", "wait_for_health_s"]}]
    assert tool_fingerprint(before) != tool_fingerprint(after)


def test_a_new_tool_changes_the_fingerprint() -> None:
    before = [{"name": "service_status", "properties": ["project"]}]
    after = [*before, {"name": "service_action", "properties": ["project", "action"]}]
    assert tool_fingerprint(before) != tool_fingerprint(after)


def test_prose_alone_does_not_change_the_fingerprint() -> None:
    """A reworded description is not a capability change."""
    entry = {"name": "fs_search", "properties": ["project", "path"]}
    assert tool_fingerprint([entry]) == tool_fingerprint([{**entry, "description": "reworded"}])


def test_the_advertised_surface_matches_the_live_catalogue() -> None:
    async def _check() -> None:
        server = create_server(transport="stdio")
        tools = await server.list_tools()
        result = await server.call_tool("server_info", {})
        assert result.structured_content is not None
        surface = result.structured_content["capabilities"]["tool_surface"]
        assert surface["count"] == len(tools)
        expected = tool_fingerprint(
            [
                {
                    "name": t.name,
                    "properties": sorted((t.parameters or {}).get("properties", {})),
                    "required": sorted((t.parameters or {}).get("required", [])),
                }
                for t in tools
            ]
        )
        assert surface["fingerprint"] == expected

    asyncio.run(_check())


def test_no_secrets_leak_through_the_new_fields() -> None:
    info = build_server_info(
        transport="stdio",
        available_tools=["server_info"],
        tool_surface=[{"name": "server_info", "properties": []}],
    )
    blob = str(info).lower()
    for forbidden in ("password", "token", "secret", "/root/", "ssh"):
        assert forbidden not in blob
