"""Start, restart and inspect the systemd unit behind a registered project.

Registered only for projects whose registry entry declares a ``control_socket``.
A deployment that declares none does not expose these tools at all.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any

from fastmcp import FastMCP

from codeagent_mcp.errors import tool_error, tool_ok
from codeagent_mcp.service_ctl import ServiceCtlError, call_service_ctl
from codeagent_mcp.tools.annotations import DEST, RO
from codeagent_mcp.workspace.projects import ProjectConfig, get_project, known_projects

_HEALTH_TIMEOUT_S = 5
_HEALTH_BODY_BYTES = 200


def _probe_health(cfg: ProjectConfig) -> dict[str, Any] | None:
    """Fetch the project's health endpoint, if it declared one."""
    if not cfg.health_url:
        return None
    try:
        # noqa justified: the registry restricts health_url to loopback http(s).
        with urllib.request.urlopen(cfg.health_url, timeout=_HEALTH_TIMEOUT_S) as resp:  # noqa: S310
            body = resp.read(_HEALTH_BODY_BYTES).decode("utf-8", "replace")
            return {"url": cfg.health_url, "http_status": resp.status, "body": body}
    except urllib.error.URLError as exc:
        return {"url": cfg.health_url, "http_status": None, "error": str(exc)}


def _controllable(project: str) -> tuple[ProjectConfig | None, dict[str, Any] | None]:
    """Resolve a project that declares a control socket, or return a tool error."""
    cfg = get_project(project)
    if cfg is None:
        return None, tool_error(
            "INVALID_ARGUMENT",
            f"unknown project {project!r}; known={list(known_projects())}",
            retryable=False,
        )
    if not cfg.control_socket:
        return None, tool_error(
            "INVALID_ARGUMENT",
            f"project {project!r} has no control_socket in the registry",
            retryable=False,
            next_action=(
                "Ask the operator to add control_socket to this project, or use "
                f"one of {list(controllable_projects())}"
            ),
        )
    return cfg, None


def controllable_projects() -> tuple[str, ...]:
    """Registered projects that declare a control socket."""
    out = []
    for name in known_projects():
        cfg = get_project(name)
        if cfg is not None and cfg.control_socket:
            out.append(name)
    return tuple(out)


def register_service_control_tools(server: FastMCP) -> None:
    """Register the service control tools when any project declares a socket."""
    try:
        if not controllable_projects():
            return
    except (OSError, ValueError):
        # A malformed or missing registry is reported by the tools that need it;
        # it must not stop the rest of the server from coming up.
        return

    @server.tool(
        name="service_status",
        description=(
            "Status of the systemd unit behind a registered project (fixed command). "
            "Returns unit state, recent journal lines with secrets redacted, and the "
            "project's health endpoint. Cannot target other units."
        ),
        annotations=RO,
    )
    def service_status(project: str) -> dict[str, Any]:
        cfg, err = _controllable(project)
        if err is not None or cfg is None:
            return err or {}
        try:
            raw = call_service_ctl(cfg.control_socket or "", "STATUS")
        except ServiceCtlError as exc:
            return tool_error(
                "SERVICE_CTL_FAILED",
                str(exc),
                retryable=exc.retryable,
                next_action="Ask an admin to check the project's .socket and .service units",
            )
        return tool_ok(project=project, output=raw, health=_probe_health(cfg))

    @server.tool(
        name="service_restart",
        description=(
            "Restart the systemd unit behind a registered project (fixed command via a "
            "privileged socket). Does not restart the MCP server, the reverse proxy or "
            "the host. Returns status, logs and health."
        ),
        annotations=DEST,
    )
    def service_restart(project: str) -> dict[str, Any]:
        cfg, err = _controllable(project)
        if err is not None or cfg is None:
            return err or {}
        try:
            raw = call_service_ctl(cfg.control_socket or "", "RESTART", timeout_s=50.0)
        except ServiceCtlError as exc:
            return tool_error(
                "SERVICE_CTL_FAILED",
                str(exc),
                retryable=exc.retryable,
                next_action="Inspect the unit's journal as an admin — the MCP cannot elevate",
            )
        restarted = "restart_ok" in raw or "ActiveState=active" in raw
        payload: dict[str, Any] = {
            "project": project,
            "restart_ok": restarted,
            "output": raw,
            "health": _probe_health(cfg),
        }
        if not restarted:
            return tool_error(
                "SERVICE_RESTART_FAILED",
                f"the unit for project {project!r} did not report active after restart",
                retryable=True,
                next_action="Read output/logs, fix the app, then retry service_restart",
                **payload,
            )
        return tool_ok(**payload)

    @server.tool(
        name="service_start",
        description=(
            "Start the systemd unit behind a registered project if it is inactive "
            "(fixed command). Prefer service_restart after changing code."
        ),
        annotations=DEST,
    )
    def service_start(project: str) -> dict[str, Any]:
        cfg, err = _controllable(project)
        if err is not None or cfg is None:
            return err or {}
        try:
            raw = call_service_ctl(cfg.control_socket or "", "START", timeout_s=50.0)
        except ServiceCtlError as exc:
            return tool_error("SERVICE_CTL_FAILED", str(exc), retryable=exc.retryable)
        return tool_ok(project=project, output=raw, health=_probe_health(cfg))
