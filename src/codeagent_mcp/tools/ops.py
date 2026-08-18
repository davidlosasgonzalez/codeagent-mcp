"""Ops tools: cleanup status and orphan detection. No secrets."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from codeagent_mcp.cleanup import detect_orphans, find_orphan_browsers, run_startup_cleanup
from codeagent_mcp.errors import tool_ok
from codeagent_mcp.tools.annotations import DEST, RO


def register_ops_tools(server: FastMCP) -> None:
    @server.tool(
        name="ops_status",
        description=(
            "Operational status: orphan lease/terminal hints, browser and exec-gate state, "
            "and detached browser processes. "
            "Does not reveal secrets, tokens, or full transcripts."
        ),
        annotations=RO,
    )
    def ops_status() -> dict[str, Any]:
        from codeagent_mcp.browser.service import get_browser_service
        from codeagent_mcp.exec.gate import get_exec_gate

        browser = get_browser_service()
        return tool_ok(
            orphans=detect_orphans(),
            browser={"running": browser.is_running(), "owner_lease_id": browser.owner_lease_id()},
            detached_browsers=find_orphan_browsers(),
            exec_gate=get_exec_gate().status(),
        )

    @server.tool(
        name="ops_cleanup",
        description=(
            "Run safe cleanup: expired artifacts, aged spool files, orphan detection, a browser "
            "whose lease is gone, stale exec-gate entries, and detached browser processes. "
            "Does not kill tmux panes or delete configured project roots."
        ),
        annotations=DEST,
    )
    def ops_cleanup() -> dict[str, Any]:
        result = run_startup_cleanup()
        return tool_ok(**result)
