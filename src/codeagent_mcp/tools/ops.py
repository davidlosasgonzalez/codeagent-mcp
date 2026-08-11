"""Ops tools: cleanup status and orphan detection. No secrets."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from codeagent_mcp.cleanup import detect_orphans, run_startup_cleanup
from codeagent_mcp.errors import tool_ok
from codeagent_mcp.tools.annotations import DEST, RO


def register_ops_tools(server: FastMCP) -> None:
    @server.tool(
        name="ops_status",
        description=(
            "Operational status: orphan lease/terminal hints and last cleanup counters. "
            "Does not reveal secrets, tokens, or full transcripts."
        ),
        annotations=RO,
    )
    def ops_status() -> dict[str, Any]:
        orphans = detect_orphans()
        return tool_ok(orphans=orphans)

    @server.tool(
        name="ops_cleanup",
        description=(
            "Run safe cleanup: expired artifacts + aged spool files + orphan detection. "
            "Does not kill tmux panes or delete configured project roots."
        ),
        annotations=DEST,
    )
    def ops_cleanup() -> dict[str, Any]:
        result = run_startup_cleanup()
        return tool_ok(**result)
