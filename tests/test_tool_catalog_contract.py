"""Contract snapshot for MCP tools/list (names, schemas, annotations).

Annotations are metadata/UX only — security is enforced by lease/path/write
gates, never by ToolAnnotations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from codeagent_mcp.server import create_server

# Conservative annotation profile per tool (readOnly, destructive, idempotent, openWorld).
EXPECTED_ANNOTATIONS: dict[str, tuple[bool, bool, bool, bool]] = {
    "browser_action": (False, False, False, False),
    "browser_ensure": (False, False, False, False),
    "browser_open": (False, False, False, False),
    "browser_reload": (False, False, False, False),
    "browser_set_viewport": (False, False, False, False),
    "browser_snapshot": (True, False, True, False),
    "exec_run": (False, True, False, True),
    "fs_apply_patch": (False, True, False, False),
    "fs_list": (True, False, True, False),
    "fs_read": (True, False, True, False),
    "fs_search": (True, False, True, False),
    "fs_stat": (True, False, True, False),
    "fs_write_binary": (False, True, False, False),
    "fs_write_file": (False, True, False, True),
    "git_diff": (True, False, True, False),
    "git_status": (True, False, True, False),
    "ops_cleanup": (False, True, False, False),
    "ops_status": (True, False, True, False),
    "project_bootstrap": (True, False, True, False),
    "project_instructions": (True, False, True, False),
    "project_skill_read": (True, False, True, False),
    "project_skills_list": (True, False, True, False),
    "server_info": (True, False, True, False),
    "terminal_close": (False, False, False, False),
    "terminal_create": (False, False, False, False),
    "terminal_interrupt": (False, False, False, False),
    "terminal_key": (False, False, False, False),
    "terminal_list": (True, False, True, False),
    "terminal_read": (True, False, True, False),
    "terminal_reset": (False, False, False, False),
    "terminal_snapshot": (True, False, True, False),
    "terminal_status": (True, False, True, False),
    "terminal_write": (False, False, False, False),
    "visual_capture": (False, False, False, False),
    "visual_compare": (False, False, False, False),
    "visual_get": (True, False, True, False),
    "workspace_acquire": (False, False, False, False),
    "workspace_release": (False, False, False, False),
    "workspace_status": (True, False, True, False),
}


def _schema_fp(parameters: dict[str, Any]) -> str:
    blob = json.dumps(parameters, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _row(tool: Any) -> dict[str, Any]:
    ann = tool.annotations
    assert ann is not None, f"{tool.name} missing annotations"
    params = tool.parameters or {}
    return {
        "name": tool.name,
        "readOnlyHint": ann.readOnlyHint,
        "destructiveHint": ann.destructiveHint,
        "idempotentHint": ann.idempotentHint,
        "openWorldHint": ann.openWorldHint,
        "properties": sorted((params.get("properties") or {}).keys()),
        "required": sorted(params.get("required") or []),
        "schema_fp": _schema_fp(params),
        "description": tool.description or "",
    }


async def _catalog(transport: str) -> list[dict[str, Any]]:
    server = create_server(transport=transport)  # type: ignore[arg-type]
    tools = await server.list_tools()
    return [_row(t) for t in sorted(tools, key=lambda x: x.name)]


def test_tool_catalog_annotations_and_names() -> None:
    rows = asyncio.run(_catalog("stdio"))
    names = [r["name"] for r in rows]
    assert names == sorted(EXPECTED_ANNOTATIONS.keys())
    for row in rows:
        expected = EXPECTED_ANNOTATIONS[row["name"]]
        got = (
            row["readOnlyHint"],
            row["destructiveHint"],
            row["idempotentHint"],
            row["openWorldHint"],
        )
        assert got == expected, f"{row['name']}: {got} != {expected}"
        # Mutators must never claim read-only.
        if got[0] is True:
            assert got[1] is False
            assert got[2] is True
        else:
            assert got[0] is False


def test_tool_catalog_stdio_http_equivalent() -> None:
    stdio = asyncio.run(_catalog("stdio"))
    http = asyncio.run(_catalog("http"))
    keys = (
        "name",
        "readOnlyHint",
        "destructiveHint",
        "idempotentHint",
        "openWorldHint",
        "properties",
        "required",
        "schema_fp",
        "description",
    )
    a = [{k: r[k] for k in keys} for r in stdio]
    b = [{k: r[k] for k in keys} for r in http]
    assert a == b


def test_tool_catalog_schema_snapshot() -> None:
    """Detect accidental schema drift (update deliberately when APIs change)."""
    rows = asyncio.run(_catalog("stdio"))
    # Fingerprint of name + property set + required + schema hash.
    snapshot = {
        r["name"]: {
            "properties": r["properties"],
            "required": r["required"],
            "schema_fp": r["schema_fp"],
        }
        for r in rows
    }
    # Stable expected fingerprints; update deliberately when a schema changes.
    expected_fp = {
        "browser_action": "fb643df425e06fd0",
        "browser_ensure": "d52bcbb2643c5191",
        "browser_open": "d44a3a2263d770e5",
        "browser_reload": "bbea661314c26888",
        "browser_set_viewport": "e00d7d1236e03439",
        "browser_snapshot": "e0d7e26ee1ba2a92",
        "exec_run": "cc323ce4a3830792",
        "fs_apply_patch": "3c5c266c1cb9e325",
        "fs_list": "e46a29c432db256a",
        "fs_read": "596b418517c8c1f1",
        "fs_search": "5d0d6cb1a059eef8",
        "fs_stat": "16ff31afcd44bb23",
        "fs_write_binary": "28f97c14a1c5bf05",
        "fs_write_file": "042ec3bc3bab644d",
        "git_diff": "cfbd57df30aa1f4e",
        "git_status": "e46a29c432db256a",
        "ops_cleanup": "fdca8d9bd184204b",
        "ops_status": "fdca8d9bd184204b",
        "project_bootstrap": "deb2ea4496d9cf3c",
        "project_instructions": "d918b1d58ed79712",
        "project_skill_read": "53c7b0da20ae032d",
        "project_skills_list": "0dc10550a8df06d2",
        "server_info": "fdca8d9bd184204b",
        "terminal_close": "7a35a3abad5acd07",
        "terminal_create": "239956018cd99602",
        "terminal_interrupt": "7a35a3abad5acd07",
        "terminal_key": "173f1e1137e33c01",
        "terminal_list": "e0d7e26ee1ba2a92",
        "terminal_read": "5b9b9297aabd0d10",
        "terminal_reset": "239956018cd99602",
        "terminal_snapshot": "0e6199bc38294cef",
        "terminal_status": "7a35a3abad5acd07",
        "terminal_write": "a0751892e318ad85",
        "visual_capture": "8a0c925eb47c4829",
        "visual_compare": "f5105dc39d443e52",
        "visual_get": "e49dc8a9f9ab28a4",
        "workspace_acquire": "01188f98e3634af9",
        "workspace_release": "e0d7e26ee1ba2a92",
        "workspace_status": "0cd141f632a37230",
    }
    assert set(snapshot) == set(expected_fp)
    for name, fp in expected_fp.items():
        assert snapshot[name]["schema_fp"] == fp, (
            f"{name} schema changed: {snapshot[name]['schema_fp']} != {fp}; "
            f"props={snapshot[name]['properties']} req={snapshot[name]['required']}"
        )


def test_fs_write_file_openai_fileparams_meta() -> None:
    rows = asyncio.run(_catalog("stdio"))

    async def _meta() -> dict:
        server = create_server(transport="stdio")
        tools = await server.list_tools()
        tool = next(t for t in tools if t.name == "fs_write_file")
        return tool.meta or {}

    meta = asyncio.run(_meta())
    assert meta.get("openai/fileParams") == ["file"]
    assert any(r["name"] == "fs_write_file" for r in rows)


def test_annotations_are_not_security_enforcement() -> None:
    """Sanity: annotation module documents UX-only; gates live elsewhere."""
    from pathlib import Path

    from codeagent_mcp.tools import annotations as ann_mod
    from codeagent_mcp.tools import workspace as ws

    src = Path(ann_mod.__file__).read_text()
    assert "metadata/UX only" in src
    ws_src = Path(ws.__file__).read_text()
    assert "ToolAnnotations" not in ws_src
    assert "readOnlyHint" not in ws_src
