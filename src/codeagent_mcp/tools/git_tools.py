"""MCP tools: git_status / git_diff (read-only)."""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from codeagent_mcp.git.service import (
    DEFAULT_MAX_DIFF_BYTES,
    DEFAULT_MAX_ENTRIES,
)
from codeagent_mcp.git.service import (
    git_diff as git_diff_impl,
)
from codeagent_mcp.git.service import (
    git_status as git_status_impl,
)
from codeagent_mcp.tools.annotations import RO
from codeagent_mcp.tools.workspace import get_lease_manager


def _resolve_project(project: str, lease_id: str) -> tuple[str, dict[str, Any] | None]:
    if not lease_id or not str(lease_id).strip():
        return project, None
    result = get_lease_manager().require_active(lease_id=str(lease_id).strip())
    if result.get("ok") is not True:
        return project, result
    return str(result["project"]), None


def register_git_tools(server: FastMCP) -> None:
    @server.tool(
        name="git_status",
        description=(
            "Structured read-only git status for a registered project root. "
            "Returns branch, upstream ahead/behind, staged/unstaged/untracked "
            "entries (capped). Optional path must stay under the project root. "
            "lease_id optional (renews if provided). No mutations."
        ),
        annotations=RO,
    )
    def git_status(
        project: str = "demo",
        path: str = "",
        max_entries: int = DEFAULT_MAX_ENTRIES,
        lease_id: str = "",
    ) -> dict[str, Any]:
        project, err = _resolve_project(project, lease_id)
        if err is not None:
            return err
        return git_status_impl(project=project, path=path, max_entries=max_entries)

    @server.tool(
        name="git_diff",
        description=(
            "Structured read-only git diff for a registered project root. "
            "mode=unstaged|staged|both. Returns numstat summary, file list, "
            "and a byte-capped unified diff with truncated=true when clipped. "
            "Optional path under root. lease_id optional. No mutations. "
            "For uncovered Git ops use exec_run."
        ),
        annotations=RO,
    )
    def git_diff(
        project: str = "demo",
        path: str = "",
        mode: Literal["unstaged", "staged", "both"] = "both",
        max_bytes: int = DEFAULT_MAX_DIFF_BYTES,
        lease_id: str = "",
    ) -> dict[str, Any]:
        project, err = _resolve_project(project, lease_id)
        if err is not None:
            return err
        return git_diff_impl(project=project, path=path, mode=mode, max_bytes=max_bytes)
