"""Read-only inspection of the data a deployed service actually runs on.

A checkout answers "what does the code say"; it does not answer "what is in the
database the running service reads". Proving a migration or a reseed landed in
production used to require either credentials or a general filesystem escape,
so it did not get proved at all.

These tools give the narrow version instead: the operator names directories in
the project registry (``runtime_paths``), and callers may list and read files
under exactly those. No writes, no execution, no path the operator did not type.
Reading still needs POSIX permission — the declaration authorizes, it does not
grant.

Registered only when at least one project declares ``runtime_paths``; a
deployment that declares none does not expose these tools at all.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from codeagent_mcp.errors import tool_error, tool_ok
from codeagent_mcp.fs.service import (
    DEFAULT_MAX_LIST,
    DEFAULT_MAX_READ_BYTES,
    FsService,
)
from codeagent_mcp.tools.annotations import RO
from codeagent_mcp.tools.workspace import get_lease_manager
from codeagent_mcp.workspace.projects import ProjectConfig, get_project, known_projects


def projects_with_runtime_paths() -> tuple[str, ...]:
    """Registered projects that declare at least one read-only runtime view."""
    out = []
    for name in known_projects():
        cfg = get_project(name)
        if cfg is not None and cfg.runtime_paths:
            out.append(name)
    return tuple(out)


def _resolve_project(project: str, lease_id: str) -> tuple[str, dict[str, Any] | None]:
    if not lease_id or not str(lease_id).strip():
        return project, None
    result = get_lease_manager().require_active(lease_id=str(lease_id).strip())
    if result.get("ok") is not True:
        return project, result
    return str(result["project"]), None


def _view(project: str, name: str) -> tuple[ProjectConfig | None, str, dict[str, Any] | None]:
    """Resolve (project, view name) to an absolute declared root, or a tool error."""
    cfg = get_project(project)
    if cfg is None:
        return (
            None,
            "",
            tool_error(
                "UNKNOWN_PROJECT",
                f"unknown project {project!r}; known={list(known_projects())}",
                retryable=False,
            ),
        )
    if not cfg.runtime_paths:
        return (
            None,
            "",
            tool_error(
                "INVALID_ARGUMENT",
                f"project {project!r} declares no runtime_paths in the registry",
                retryable=False,
                next_action=(
                    "Ask the operator to declare one, or use a project from "
                    f"{list(projects_with_runtime_paths())}"
                ),
            ),
        )
    root = cfg.runtime_paths.get(str(name).strip())
    if not root:
        return (
            None,
            "",
            tool_error(
                "NOT_FOUND",
                f"project {project!r} has no runtime path named {name!r}",
                retryable=False,
                next_action=f"Use one of {sorted(cfg.runtime_paths)}",
            ),
        )
    return cfg, root, None


def register_runtime_tools(server: FastMCP) -> None:
    """Register the runtime inspection tools when any project declares a view."""
    try:
        if not projects_with_runtime_paths():
            return
    except (OSError, ValueError):
        # A malformed or missing registry is reported by the tools that need it;
        # it must not stop the rest of the server from coming up.
        return

    @server.tool(
        name="runtime_list",
        description=(
            "List the read-only runtime views an operator declared for a project, or the "
            "entries under one of them. Call with no name to see the declared views; pass "
            "name (and optional path inside it) to list a directory. Confined to the "
            "declared roots — never the project checkout, never the wider filesystem. "
            "Read-only; lease_id optional."
        ),
        annotations=RO,
    )
    def runtime_list(
        project: str = "demo",
        name: str = "",
        path: str = "",
        max_entries: int = DEFAULT_MAX_LIST,
        lease_id: str = "",
    ) -> dict[str, Any]:
        project, err = _resolve_project(project, lease_id)
        if err is not None:
            return err
        if not str(name).strip():
            cfg = get_project(project)
            if cfg is None:
                return tool_error(
                    "UNKNOWN_PROJECT",
                    f"unknown project {project!r}; known={list(known_projects())}",
                    retryable=False,
                )
            return tool_ok(
                project=cfg.name,
                views=[{"name": k, "path": v} for k, v in sorted(cfg.runtime_paths.items())],
                count=len(cfg.runtime_paths),
                note="Pass name= to list inside a view. Views are read-only.",
            )
        cfg, root, err = _view(project, name)
        if err is not None or cfg is None:
            return err or {}
        result = FsService(project=cfg.name, root=root).list_dir(path, max_entries=max_entries)
        if result.get("ok") is True:
            result["view"] = str(name).strip()
        return result

    @server.tool(
        name="runtime_read",
        description=(
            "Read a file under one of a project's declared read-only runtime views "
            "(name from runtime_list). Byte-capped, no writes, no execution. Use it to "
            "verify what the deployed service actually holds instead of asserting it "
            "from the checkout. Read-only; lease_id optional."
        ),
        annotations=RO,
    )
    def runtime_read(
        name: str,
        path: str,
        project: str = "demo",
        max_bytes: int = DEFAULT_MAX_READ_BYTES,
        lease_id: str = "",
    ) -> dict[str, Any]:
        project, err = _resolve_project(project, lease_id)
        if err is not None:
            return err
        cfg, root, err = _view(project, name)
        if err is not None or cfg is None:
            return err or {}
        result = FsService(project=cfg.name, root=root).read(path, max_bytes=max_bytes)
        if result.get("ok") is True:
            result["view"] = str(name).strip()
        return result
