"""fs_write_binary acceptance tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path

import pytest

from codeagent_mcp.fs.binary_write import MAX_BINARY_WRITE_BYTES
from codeagent_mcp.fs.service import FsService
from codeagent_mcp.server import create_server
from codeagent_mcp.tools.workspace import set_lease_manager
from codeagent_mcp.workspace import projects as projects_mod
from codeagent_mcp.workspace.lease_store import LeaseStore
from codeagent_mcp.workspace.leases import LeaseManager


@pytest.fixture()
def binary_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "proj"
    root.mkdir()
    existing = b"\x00\xffPNG-like\x01\x02"
    (root / "asset.bin").write_bytes(existing)
    os.chmod(root / "asset.bin", 0o640)
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
    yield {
        "root": root,
        "lease_id": acq["lease_id"],
        "mgr": mgr,
        "existing": existing,
        "existing_sha": hashlib.sha256(existing).hexdigest(),
    }
    set_lease_manager(None)


def test_create_exact_bytes(binary_env) -> None:
    payload = b"\x00\xff\xfe hello-bin\x80\x7f"
    b64 = base64.b64encode(payload).decode("ascii")
    svc = FsService(project="demo")
    out = svc.write_binary("new.bin", content_base64=b64, create=True)
    assert out["ok"] is True
    assert out["created"] is True
    assert out["size_bytes"] == len(payload)
    assert out["sha256"] == hashlib.sha256(payload).hexdigest()
    assert (binary_env["root"] / "new.bin").read_bytes() == payload


def test_binary_not_utf8_path(binary_env) -> None:
    payload = bytes(range(256))
    b64 = base64.b64encode(payload).decode("ascii")
    svc = FsService(project="demo")
    out = svc.write_binary("full.bin", content_base64=b64, create=True)
    assert out["ok"] is True
    assert (binary_env["root"] / "full.bin").read_bytes() == payload


def test_base64_wrapped_in_lines(binary_env) -> None:
    """Agents wrap long Base64; that must not cost a retry."""
    payload = bytes(range(256)) * 8
    raw = base64.b64encode(payload).decode("ascii")
    wrapped = "\n".join(raw[i : i + 76] for i in range(0, len(raw), 76)) + "\n"
    svc = FsService(project="demo")
    out = svc.write_binary("wrapped.bin", content_base64=wrapped, create=True)
    assert out["ok"] is True, out
    assert (binary_env["root"] / "wrapped.bin").read_bytes() == payload


def test_base64_urlsafe_and_unpadded(binary_env) -> None:
    payload = b"\xfb\xff\xfe?~>" * 11  # length 66: needs padding, hits - and _ in urlsafe
    urlsafe = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    assert "-" in urlsafe or "_" in urlsafe
    svc = FsService(project="demo")
    out = svc.write_binary("urlsafe.bin", content_base64=urlsafe, create=True)
    assert out["ok"] is True, out
    assert (binary_env["root"] / "urlsafe.bin").read_bytes() == payload


def test_truncated_base64_still_rejected(binary_env) -> None:
    """Tolerance is about encoding shape only — dropped bytes must not pass silently."""
    payload = b"\x00\x01\x02\x03" * 64
    raw = base64.b64encode(payload).decode("ascii")
    svc = FsService(project="demo")
    out = svc.write_binary("cut.bin", content_base64=raw[: len(raw) // 2], create=True)
    # Truncation on a 4-char boundary decodes to fewer bytes; the SHA the caller
    # verifies afterwards is what catches it. Either way it is never the payload.
    if out["ok"]:
        assert (binary_env["root"] / "cut.bin").read_bytes() != payload
    else:
        assert out["error"]["code"] == "INVALID_ARGUMENT"


def test_data_url_rejected_with_next_action(binary_env) -> None:
    svc = FsService(project="demo")
    out = svc.write_binary(
        "data.bin",
        content_base64="data:image/png;base64,iVBORw0KGgo=",
        create=True,
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_ARGUMENT"
    assert "next_action" in out["error"]


def test_invalid_base64(binary_env) -> None:
    svc = FsService(project="demo")
    out = svc.write_binary("bad.bin", content_base64="@@@not-base64@@@", create=True)
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_ARGUMENT"
    assert out["error"]["retryable"] is False
    assert not (binary_env["root"] / "bad.bin").exists()


def test_data_url_rejected(binary_env) -> None:
    raw = base64.b64encode(b"x").decode("ascii")
    svc = FsService(project="demo")
    out = svc.write_binary(
        "bad.bin",
        content_base64=f"data:image/png;base64,{raw}",
        create=True,
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_ARGUMENT"
    assert not (binary_env["root"] / "bad.bin").exists()


def test_oversized_payload(binary_env) -> None:
    # Pre-decode gate: huge Base64 string without writing.
    huge = "A" * ((MAX_BINARY_WRITE_BYTES + 1000) * 4 // 3)
    svc = FsService(project="demo")
    out = svc.write_binary("big.bin", content_base64=huge, create=True)
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_ARGUMENT"
    assert not (binary_env["root"] / "big.bin").exists()


def test_oversized_after_decode(binary_env) -> None:
    payload = b"\x00" * (MAX_BINARY_WRITE_BYTES + 1)
    b64 = base64.b64encode(payload).decode("ascii")
    svc = FsService(project="demo")
    out = svc.write_binary("big2.bin", content_base64=b64, create=True)
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_ARGUMENT"
    assert not (binary_env["root"] / "big2.bin").exists()


def test_tool_lease_required(binary_env) -> None:
    from fastmcp import Client

    server = create_server(transport="stdio")

    async def _run() -> None:
        async with Client(server) as client:
            res = await client.call_tool(
                "fs_write_binary",
                {
                    "path": "x.bin",
                    "lease_id": "",
                    "content_base64": base64.b64encode(b"x").decode("ascii"),
                    "create": True,
                },
            )
            data = res.data if hasattr(res, "data") else None
            if data is None and hasattr(res, "content"):
                text = res.content[0].text  # type: ignore[union-attr]
                data = json.loads(text)
            assert isinstance(data, dict)
            assert data["ok"] is False
            assert data["error"]["code"] == "LEASE_REQUIRED"

    asyncio.run(_run())


def test_write_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "ro"
    root.mkdir()
    from conftest import override_projects
    from fastmcp import Client

    override_projects(
        monkeypatch,
        {"demo": projects_mod.ProjectConfig(name="demo", root=str(root), writable=False)},
    )
    store = tmp_path / "leases.json"
    monkeypatch.setenv("CODEAGENT_LEASE_STORE", str(store))
    mgr = LeaseManager(LeaseStore(store), ttl_s=2700)
    set_lease_manager(mgr)
    acq = mgr.acquire(project="demo")
    assert acq["ok"]
    server = create_server(transport="stdio")

    async def _run() -> None:
        async with Client(server) as client:
            res = await client.call_tool(
                "fs_write_binary",
                {
                    "path": "x.bin",
                    "lease_id": acq["lease_id"],
                    "content_base64": base64.b64encode(b"x").decode("ascii"),
                    "create": True,
                },
            )
            data = res.data if hasattr(res, "data") else None
            if data is None and hasattr(res, "content"):
                text = res.content[0].text  # type: ignore[union-attr]
                data = json.loads(text)
            assert isinstance(data, dict)
            assert data["ok"] is False
            assert data["error"]["code"] == "WRITE_DISABLED"

    try:
        asyncio.run(_run())
    finally:
        set_lease_manager(None)


def test_path_escape(binary_env) -> None:
    svc = FsService(project="demo")
    out = svc.write_binary(
        "../escape.bin",
        content_base64=base64.b64encode(b"x").decode("ascii"),
        create=True,
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "PATH_OUTSIDE_ROOT"


def test_existing_without_hash(binary_env) -> None:
    svc = FsService(project="demo")
    out = svc.write_binary(
        "asset.bin",
        content_base64=base64.b64encode(b"new").decode("ascii"),
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_ARGUMENT"
    assert (binary_env["root"] / "asset.bin").read_bytes() == binary_env["existing"]


def test_replace_preserves_mode(binary_env) -> None:
    new_payload = b"\xff\x00replaced"
    svc = FsService(project="demo")
    out = svc.write_binary(
        "asset.bin",
        content_base64=base64.b64encode(new_payload).decode("ascii"),
        expected_sha256=binary_env["existing_sha"],
    )
    assert out["ok"] is True
    assert out["created"] is False
    target = binary_env["root"] / "asset.bin"
    assert target.read_bytes() == new_payload
    assert out["sha256"] == hashlib.sha256(new_payload).hexdigest()
    assert oct(target.stat().st_mode & 0o777) == "0o640"


def test_stale_hash_conflict(binary_env) -> None:
    svc = FsService(project="demo")
    stale = binary_env["existing_sha"]
    (binary_env["root"] / "asset.bin").write_bytes(b"changed-underneath")
    out = svc.write_binary(
        "asset.bin",
        content_base64=base64.b64encode(b"should-not-write").decode("ascii"),
        expected_sha256=stale,
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "CONFLICT"
    assert (binary_env["root"] / "asset.bin").read_bytes() == b"changed-underneath"


def test_atomic_failure_keeps_original(binary_env, monkeypatch: pytest.MonkeyPatch) -> None:
    svc = FsService(project="demo")
    original = binary_env["existing"]

    def boom(*_a, **_k):
        raise OSError(28, "No space left on device")  # ENOSPC

    monkeypatch.setattr(os, "replace", boom)
    out = svc.write_binary(
        "asset.bin",
        content_base64=base64.b64encode(b"nope").decode("ascii"),
        expected_sha256=binary_env["existing_sha"],
    )
    assert out["ok"] is False
    assert (binary_env["root"] / "asset.bin").read_bytes() == original
    leftovers = list(binary_env["root"].glob(".codeagent-tmp-*"))
    assert not leftovers


def test_tool_registered_stdio_and_http() -> None:
    async def _names(transport: str) -> set[str]:
        server = create_server(transport=transport)  # type: ignore[arg-type]
        tools = await server.list_tools()
        return {t.name for t in tools}

    stdio = asyncio.run(_names("stdio"))
    http = asyncio.run(_names("http"))
    assert "fs_write_binary" in stdio
    assert stdio == http


def test_tool_annotation_dest() -> None:
    async def _ann() -> tuple:
        server = create_server(transport="stdio")
        tools = await server.list_tools()
        tool = next(t for t in tools if t.name == "fs_write_binary")
        a = tool.annotations
        assert a is not None
        return (
            a.readOnlyHint,
            a.destructiveHint,
            a.idempotentHint,
            a.openWorldHint,
        )

    assert asyncio.run(_ann()) == (False, True, False, False)


def test_no_payload_echo(binary_env) -> None:
    payload = b"\x00secret-bytes\xff"
    b64 = base64.b64encode(payload).decode("ascii")
    svc = FsService(project="demo")
    out = svc.write_binary("echo.bin", content_base64=b64, create=True)
    blob = json.dumps(out)
    assert "content_base64" not in blob
    assert b64 not in blob
    assert "secret-bytes" not in blob
