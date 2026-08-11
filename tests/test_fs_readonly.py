"""Adversarial filesystem tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codeagent_mcp.fs.openat2 import JailError, PathJail
from codeagent_mcp.fs.service import FsService
from codeagent_mcp.server import create_server
from codeagent_mcp.tools.workspace import set_lease_manager
from codeagent_mcp.workspace import projects as projects_mod
from codeagent_mcp.workspace.lease_store import LeaseStore
from codeagent_mcp.workspace.leases import LeaseManager


@pytest.fixture()
def fs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "hello.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    (root / "subdir").mkdir()
    (root / "subdir" / "nested.txt").write_text("nested-content\n", encoding="utf-8")
    (root / "spaced name.txt").write_text("spaces ok\n", encoding="utf-8")
    (root / "unicode_ñ.txt").write_text("café\n", encoding="utf-8")
    (root / "binary.bin").write_bytes(b"hello\x00world")
    # symlink escape
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope\n", encoding="utf-8")
    (root / "escape_link").symlink_to(outside)

    from conftest import override_projects

    override_projects(
        monkeypatch,
        {"demo": projects_mod.ProjectConfig(name="demo", root=str(root))},
    )
    store = tmp_path / "leases.json"
    monkeypatch.setenv("CODEAGENT_LEASE_STORE", str(store))
    set_lease_manager(LeaseManager(LeaseStore(store), ttl_s=2700))
    yield root
    set_lease_manager(None)


def test_stat_and_list(fs_root: Path) -> None:
    svc = FsService(project="demo")
    st = svc.stat("hello.txt")
    assert st["ok"] and st["type"] == "file" and st["size_bytes"] > 0
    listing = svc.list_dir("")
    assert listing["ok"]
    names = {e["name"] for e in listing["entries"]}
    assert "hello.txt" in names and "subdir" in names


def test_read_range_and_sha(fs_root: Path) -> None:
    svc = FsService(project="demo")
    out = svc.read("hello.txt", start_line=2, end_line=2)
    assert out["ok"]
    assert "line2" in out["content"]
    assert out["sha256"]
    assert out["truncated"] is False


def test_traversal_dotdot(fs_root: Path) -> None:
    svc = FsService(project="demo")
    out = svc.read("../outside/secret.txt")
    assert out["ok"] is False
    assert out["error"]["code"] == "PATH_OUTSIDE_ROOT"


def test_symlink_escape(fs_root: Path) -> None:
    svc = FsService(project="demo")
    out = svc.read("escape_link/secret.txt")
    assert out["ok"] is False
    assert out["error"]["code"] == "PATH_OUTSIDE_ROOT"


def test_openat2_jail_direct(fs_root: Path) -> None:
    with PathJail(fs_root) as jail:
        fd = jail.open("hello.txt")
        os.close(fd)
        with pytest.raises(JailError) as ei:
            jail.open("escape_link/secret.txt")
        assert ei.value.code == "PATH_OUTSIDE_ROOT"


def test_unicode_and_spaces(fs_root: Path) -> None:
    svc = FsService(project="demo")
    assert svc.read("spaced name.txt")["ok"]
    assert svc.read("unicode_ñ.txt")["ok"]
    assert "café" in svc.read("unicode_ñ.txt")["content"]


def test_binary_rejected(fs_root: Path) -> None:
    svc = FsService(project="demo")
    out = svc.read("binary.bin")
    assert out["ok"] is False
    assert out["error"]["code"] == "UNSUPPORTED_BINARY"


def test_giant_truncated(fs_root: Path) -> None:
    big = fs_root / "big.txt"
    big.write_text(("A" * 200 + "\n") * 50, encoding="utf-8")
    svc = FsService(project="demo")
    out = svc.read("big.txt", max_bytes=100)
    assert out["ok"] is True
    assert out["truncated"] is True


def test_search_literal(fs_root: Path) -> None:
    svc = FsService(project="demo")
    out = svc.search("nested-content", literal=True)
    assert out["ok"]
    assert out["count"] >= 1
    assert any("nested" in m["relative"] for m in out["matches"])


def test_search_bad_regex(fs_root: Path) -> None:
    svc = FsService(project="demo")
    out = svc.search("(", literal=False)
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_ARGUMENT"


def test_read_without_lease_ok(fs_root: Path) -> None:
    # service path already covers; ensure lease optional at tool layer via renew helper
    from codeagent_mcp.tools import fs as fs_tools

    assert fs_tools._maybe_renew_lease("") is None


def test_invalid_lease_when_provided(fs_root: Path) -> None:
    from codeagent_mcp.tools import fs as fs_tools

    err = fs_tools._maybe_renew_lease("nope")
    assert err is not None and err["ok"] is False
    assert err["error"]["code"] == "LEASE_EXPIRED"


def test_server_registers_fs_tools(fs_root: Path) -> None:
    import asyncio

    server = create_server(transport="stdio")

    async def _names() -> list[str]:
        tools = await server.list_tools()
        return sorted(t.name for t in tools)

    names = asyncio.run(_names())
    for n in ("fs_stat", "fs_list", "fs_read", "fs_search"):
        assert n in names
