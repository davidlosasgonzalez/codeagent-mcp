"""MCP tools for exclusive workspace leases (acquire / status / release)."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from codeagent_mcp.tools.annotations import MUT, RO
from codeagent_mcp.workspace.leases import LeaseManager

_manager: LeaseManager | None = None


def get_lease_manager() -> LeaseManager:
    """Return the process-wide :class:`LeaseManager` (lazy-created from env)."""
    global _manager
    if _manager is None:
        _manager = LeaseManager.from_env()
    return _manager


def set_lease_manager(manager: LeaseManager | None) -> None:
    """Replace the process-wide manager (tests only). Pass ``None`` to reset."""
    global _manager
    _manager = manager


def _optional_holder_sub() -> str | None:
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
    except Exception:
        return None
    if token is None:
        return None
    claims = getattr(token, "claims", None) or {}
    sub = claims.get("sub")
    return str(sub) if sub else None


def register_workspace_tools(server: FastMCP) -> None:
    """Register workspace_acquire, workspace_status, and workspace_release."""

    @server.tool(
        name="workspace_acquire",
        description=(
            "Acquire or renew an exclusive workspace lease for a registered project "
            "(registered project id; default demo). Use before mutating tools. "
            "If another exclusive lease is active, returns LEASE_BUSY — do not retry blindly. "
            "Pass the same lease_id to renew by activity. Does not write project files."
        ),
        annotations=MUT,
    )
    def workspace_acquire(
        project: str = "demo",
        mode: str = "exclusive",
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        return get_lease_manager().acquire(
            project=project,
            mode=mode,
            lease_id=lease_id,
            holder_sub=_optional_holder_sub(),
        )

    @server.tool(
        name="workspace_status",
        description=(
            "Report whether a project workspace is free, held, or expired. "
            "With lease_id, reports that lease. Does not reveal another holder's lease_id. "
            "Use after LEASE_BUSY or before acquire."
        ),
        annotations=RO,
    )
    def workspace_status(
        project: str | None = None,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        return get_lease_manager().status(
            project=project,
            lease_id=lease_id,
            holder_sub=_optional_holder_sub(),
        )

    @server.tool(
        name="workspace_release",
        description=(
            "Release an exclusive workspace lease by lease_id. Idempotent. "
            "Does not kill terminal sessions or processes. "
            "Do not use to steal another writer's lease unless you possess their lease_id."
        ),
        annotations=MUT,
    )
    def workspace_release(lease_id: str) -> dict[str, Any]:
        return get_lease_manager().release(lease_id=lease_id)
