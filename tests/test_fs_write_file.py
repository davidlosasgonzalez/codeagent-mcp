"""fs_write_file (ChatGPT fileParams → Core write_bytes)."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest

from codeagent_mcp.adaptation import chatgpt_file_download as dl
from codeagent_mcp.fs.binary_write import MAX_BINARY_WRITE_BYTES
from codeagent_mcp.server import create_server
from codeagent_mcp.tools.workspace import set_lease_manager
from codeagent_mcp.workspace import projects as projects_mod
from codeagent_mcp.workspace.lease_store import LeaseStore
from codeagent_mcp.workspace.leases import LeaseManager


@pytest.fixture()
def file_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "proj"
    root.mkdir()
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
    yield {"root": root, "lease_id": acq["lease_id"]}
    set_lease_manager(None)


def test_fs_write_file_create(file_env, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"\x89PNG\r\n\x1a\n-fake"

    def fake_download(ref: dl.OpenAIFileRef, *, max_bytes: int = MAX_BINARY_WRITE_BYTES) -> bytes:
        assert ref.file_id == "file_xyz"
        assert max_bytes == MAX_BINARY_WRITE_BYTES
        return payload

    monkeypatch.setattr(
        "codeagent_mcp.tools.fs.download_openai_file_bytes",
        fake_download,
    )

    async def _call() -> dict[str, Any]:
        server = create_server(transport="stdio")
        result = await server.call_tool(
            "fs_write_file",
            {
                "path": "out.png",
                "lease_id": file_env["lease_id"],
                "file": {
                    "download_url": "https://files.oaiusercontent.com/a?sig=1",
                    "file_id": "file_xyz",
                    "mime_type": "image/png",
                    "file_name": "out.png",
                },
                "create": True,
            },
        )
        assert result.structured_content is not None
        return result.structured_content

    out = asyncio.run(_call())
    assert out["ok"] is True
    assert out["created"] is True
    assert out["sha256"] == hashlib.sha256(payload).hexdigest()
    assert (file_env["root"] / "out.png").read_bytes() == payload


def test_fs_write_file_rechecks_lease_after_download(
    file_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Download may take time; expired/released lease must not write."""

    def fake_download(ref: dl.OpenAIFileRef, *, max_bytes: int = MAX_BINARY_WRITE_BYTES) -> bytes:
        _ = ref, max_bytes
        # Release while "downloading".
        from codeagent_mcp.tools.workspace import get_lease_manager

        get_lease_manager().release(lease_id=file_env["lease_id"])
        return b"late-bytes"

    monkeypatch.setattr(
        "codeagent_mcp.tools.fs.download_openai_file_bytes",
        fake_download,
    )

    async def _call() -> dict[str, Any]:
        server = create_server(transport="stdio")
        result = await server.call_tool(
            "fs_write_file",
            {
                "path": "late.bin",
                "lease_id": file_env["lease_id"],
                "file": {
                    "download_url": "https://files.oaiusercontent.com/a",
                    "file_id": "file_late",
                },
                "create": True,
            },
        )
        assert result.structured_content is not None
        return result.structured_content

    out = asyncio.run(_call())
    assert out["ok"] is False
    assert out["error"]["code"] == "LEASE_EXPIRED"
    assert not (file_env["root"] / "late.bin").exists()


def test_fs_write_file_from_sandbox_reference(file_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """ImageGen → /mnt/data → repo, the flow that used to fail with RISK_BLOCKED.

    Only the TCP/TLS hop is faked: host allowlisting, URL validation and the
    lease/SHA gates all run for real against a sandbox-style file reference.
    """
    payload = b"\xff\xd8\xff\xe0" + b"jpeg-scanlines" * 4000  # ~56KB: full-quality, not a stub
    captured: dict[str, Any] = {}

    def fake_pinned(*, hostname: str, path_query: str, port: int, ip: str, max_bytes: int) -> bytes:
        captured.update(hostname=hostname, path_query=path_query, port=port, ip=ip)
        assert max_bytes == MAX_BINARY_WRITE_BYTES
        return payload

    monkeypatch.setattr(dl, "_resolve_public_ips", lambda _h: ["20.60.40.4"])
    monkeypatch.setattr(dl, "_https_get_pinned", fake_pinned)

    async def _call() -> dict[str, Any]:
        server = create_server(transport="stdio")
        result = await server.call_tool(
            "fs_write_file",
            {
                "path": "assets/img/diagram.jpg",
                "lease_id": file_env["lease_id"],
                "file": {
                    "download_url": (
                        "https://oaisdmntprnortheu.blob.core.windows.net/"
                        "files/00000000/raw?se=2026-08-11&sig=SECRET"
                    ),
                    "file_id": "file-abc123",
                    "mime_type": "image/jpeg",
                    "file_name": "generated-image.jpg",
                },
                "create": True,
            },
        )
        assert result.structured_content is not None
        return result.structured_content

    target = file_env["root"] / "assets/img"
    target.mkdir(parents=True)

    out = asyncio.run(_call())
    assert out["ok"] is True, out
    assert captured["hostname"] == "oaisdmntprnortheu.blob.core.windows.net"
    assert captured["port"] == 443
    # The signed query must survive the round trip (it is the capability)...
    assert "sig=SECRET" in captured["path_query"]
    # ...but path is authoritative: file_name never decides where bytes land.
    written = target / "diagram.jpg"
    assert written.read_bytes() == payload
    assert out["sha256"] == hashlib.sha256(payload).hexdigest()
    assert out["size_bytes"] > 50_000


def test_fs_write_file_arbitrary_host_blocked(file_env) -> None:
    """Widening the allowlist for the sandbox must not make this a generic downloader."""

    async def _call() -> dict[str, Any]:
        server = create_server(transport="stdio")
        result = await server.call_tool(
            "fs_write_file",
            {
                "path": "grabbed.bin",
                "lease_id": file_env["lease_id"],
                "file": {
                    "download_url": "https://cdn.example.com/payload.bin",
                    "file_id": "file_x",
                },
                "create": True,
            },
        )
        assert result.structured_content is not None
        return result.structured_content

    out = asyncio.run(_call())
    assert out["ok"] is False
    assert out["error"]["code"] == "RISK_BLOCKED"
    assert not (file_env["root"] / "grabbed.bin").exists()


def test_fs_write_file_ssrf_blocked(file_env) -> None:
    async def _call() -> dict[str, Any]:
        server = create_server(transport="stdio")
        result = await server.call_tool(
            "fs_write_file",
            {
                "path": "evil.bin",
                "lease_id": file_env["lease_id"],
                "file": {
                    "download_url": "https://127.0.0.1/secret",
                    "file_id": "file_x",
                },
                "create": True,
            },
        )
        assert result.structured_content is not None
        return result.structured_content

    out = asyncio.run(_call())
    assert out["ok"] is False
    assert out["error"]["code"] == "RISK_BLOCKED"
    assert not (file_env["root"] / "evil.bin").exists()


def test_fs_write_file_meta_and_schema() -> None:
    async def _check() -> None:
        server = create_server(transport="stdio")
        tools = await server.list_tools()
        tool = next(t for t in tools if t.name == "fs_write_file")
        meta = tool.meta or {}
        assert meta.get("openai/fileParams") == ["file"]
        assert tool.annotations is not None
        assert tool.annotations.openWorldHint is True
        assert tool.annotations.destructiveHint is True
        params = tool.parameters or {}
        props = params.get("properties") or {}
        assert "file" in props
        assert "path" in props
        assert "lease_id" in props
        # Resolve $ref if present
        file_schema = props["file"]
        if "$ref" in file_schema:
            ref = file_schema["$ref"].rsplit("/", 1)[-1]
            file_schema = (params.get("$defs") or params.get("definitions") or {})[ref]
        assert set(file_schema.get("required") or []) == {"download_url", "file_id"}
        fprops = file_schema.get("properties") or {}
        assert set(fprops) == {"download_url", "file_id", "mime_type", "file_name"}
        for key in ("mime_type", "file_name", "download_url", "file_id"):
            t = fprops[key].get("type")
            # Must be string, not ["string","null"]
            assert t == "string" or t == ["string"], f"{key} type={t}"

    asyncio.run(_check())


def test_write_bytes_oversized_direct(file_env) -> None:
    from codeagent_mcp.fs.service import FsService

    huge = b"x" * (MAX_BINARY_WRITE_BYTES + 1)
    out = FsService(project="demo").write_bytes("big.bin", data=huge, create=True)
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_ARGUMENT"
