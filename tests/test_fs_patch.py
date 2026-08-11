"""fs_apply_patch acceptance tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codeagent_mcp.fs.service import FsService
from codeagent_mcp.server import create_server
from codeagent_mcp.tools.workspace import set_lease_manager
from codeagent_mcp.workspace import projects as projects_mod
from codeagent_mcp.workspace.lease_store import LeaseStore
from codeagent_mcp.workspace.leases import LeaseManager


@pytest.fixture()
def patch_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("hello world\nsecond line\n", encoding="utf-8")
    (root / "uni.txt").write_text("café\n", encoding="utf-8")
    from conftest import override_projects

    override_projects(
        monkeypatch,
        {"demo": projects_mod.ProjectConfig(name="demo", root=str(root), writable=True)},
    )
    store = tmp_path / "leases.json"
    monkeypatch.setenv("CODEAGENT_LEASE_STORE", str(store))
    mgr = LeaseManager(LeaseStore(store), ttl_s=2700)
    set_lease_manager(mgr)
    acq = mgr.acquire(project="demo")
    assert acq["ok"]
    yield {"root": root, "lease_id": acq["lease_id"], "mgr": mgr}
    set_lease_manager(None)


def test_small_edit(patch_env) -> None:
    svc = FsService(project="demo")
    before = svc.read("a.txt")
    out = svc.apply_patch(
        "a.txt",
        expected_sha256=before["sha256"],
        edits=[{"old_string": "hello world", "new_string": "hello codeagent"}],
    )
    assert out["ok"] is True
    assert "hello codeagent" in (patch_env["root"] / "a.txt").read_text(encoding="utf-8")
    assert out["sha256"] != before["sha256"]


def test_multi_hunk(patch_env) -> None:
    svc = FsService(project="demo")
    before = svc.read("a.txt")
    out = svc.apply_patch(
        "a.txt",
        expected_sha256=before["sha256"],
        edits=[
            {"old_string": "hello world", "new_string": "A"},
            {"old_string": "second line", "new_string": "B"},
        ],
    )
    assert out["ok"]
    text = (patch_env["root"] / "a.txt").read_text(encoding="utf-8")
    assert text == "A\nB\n"


def test_unicode(patch_env) -> None:
    svc = FsService(project="demo")
    before = svc.read("uni.txt")
    out = svc.apply_patch(
        "uni.txt",
        expected_sha256=before["sha256"],
        edits=[{"old_string": "café", "new_string": "cafés"}],
    )
    assert out["ok"]
    assert "cafés" in (patch_env["root"] / "uni.txt").read_text(encoding="utf-8")


def test_conflict(patch_env) -> None:
    svc = FsService(project="demo")
    before = svc.read("a.txt")
    (patch_env["root"] / "a.txt").write_text("changed underneath\n", encoding="utf-8")
    out = svc.apply_patch(
        "a.txt",
        expected_sha256=before["sha256"],
        edits=[{"old_string": "hello", "new_string": "x"}],
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "CONFLICT"


def test_invalid_edit(patch_env) -> None:
    svc = FsService(project="demo")
    before = svc.read("a.txt")
    out = svc.apply_patch(
        "a.txt",
        expected_sha256=before["sha256"],
        edits=[{"old_string": "missing-token-xyz", "new_string": "x"}],
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_ARGUMENT"


def test_create_file(patch_env) -> None:
    svc = FsService(project="demo")
    out = svc.apply_patch(
        "new.txt",
        expected_sha256="",
        new_content="brand new\n",
        create=True,
    )
    assert out["ok"] and out["created"] is True
    assert (patch_env["root"] / "new.txt").read_text(encoding="utf-8") == "brand new\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory mode bits")
def test_permission_denied(patch_env) -> None:
    svc = FsService(project="demo")
    before = svc.read("a.txt")
    os.chmod(patch_env["root"], 0o555)
    try:
        out = svc.apply_patch(
            "a.txt",
            expected_sha256=before["sha256"],
            edits=[{"old_string": "hello world", "new_string": "nope"}],
        )
        assert out["ok"] is False
        assert out["error"]["code"] == "PERMISSION_DENIED"
        # target unchanged
        assert "hello world" in (patch_env["root"] / "a.txt").read_text(encoding="utf-8")
    finally:
        os.chmod(patch_env["root"], 0o755)


def test_path_escape(patch_env) -> None:
    svc = FsService(project="demo")
    out = svc.apply_patch(
        "../escape.txt",
        expected_sha256="",
        new_content="x",
        create=True,
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "PATH_OUTSIDE_ROOT"


def test_tool_lease_gate(patch_env) -> None:
    import asyncio

    from fastmcp import Client

    server = create_server(transport="stdio")

    async def _run() -> None:
        async with Client(server) as client:
            tools = {t.name for t in await client.list_tools()}
            assert "fs_apply_patch" in tools
            res = await client.call_tool(
                "fs_apply_patch",
                {
                    "path": "a.txt",
                    "lease_id": "",
                    "edits": [{"old_string": "a", "new_string": "b"}],
                },
            )
            # FastMCP wraps; get structured content
            data = res.data if hasattr(res, "data") else None
            if data is None and hasattr(res, "content"):
                # fallback parse
                import json
                from typing import Any, cast

                text = cast("Any", res.content[0]).text
                data = json.loads(text)
            assert data is not None
            assert data["ok"] is False
            assert data["error"]["code"] == "LEASE_REQUIRED"

    asyncio.run(_run())


def test_server_lists_apply(patch_env) -> None:
    import asyncio

    server = create_server(transport="stdio")

    async def _names() -> list[str]:
        return sorted(t.name for t in await server.list_tools())

    assert "fs_apply_patch" in asyncio.run(_names())
