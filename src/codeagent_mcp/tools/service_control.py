"""Start, restart and inspect the systemd unit behind a registered project.

Registered only for projects whose registry entry declares a ``control_socket``.
A deployment that declares none does not expose these tools at all.
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from fastmcp import FastMCP

from codeagent_mcp.errors import tool_error, tool_ok
from codeagent_mcp.exec.runas import split_exit_code
from codeagent_mcp.redact import redact, redaction_count
from codeagent_mcp.service_ctl import ServiceCtlError, call_service_ctl
from codeagent_mcp.tools.annotations import DEST, RO
from codeagent_mcp.workspace.projects import (
    _LOOPBACK_HOSTS,
    ProjectConfig,
    get_project,
    known_projects,
)

DEFAULT_LOG_LINES = 60
MAX_LOG_LINES = 300
# A unit name reaching the helper is validated there too; this only keeps
# obvious junk off the wire.
_SAFE_UNIT_RE = re.compile(r"^[A-Za-z0-9@._-]{1,64}$")
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ACTION_TIMEOUT_S = 320.0

MAX_HEALTH_WAIT_S = 120


def _health_ready(health: dict[str, Any] | None) -> bool | None:
    """True/False when a health URL was declared and probed, None when it was not.

    None is not False. A project with no health endpoint has not failed a check;
    it has no check, and saying otherwise would invent a red light.
    """
    if not health:
        return None
    status = health.get("http_status")
    return bool(isinstance(status, int) and 200 <= status < 400)


def _await_health(cfg: ProjectConfig, wait_s: float) -> dict[str, Any] | None:
    """Probe until the endpoint answers or the budget runs out."""
    health = _probe_health(cfg)
    if not cfg.health_url or wait_s <= 0:
        return health
    deadline = time.monotonic() + min(float(wait_s), MAX_HEALTH_WAIT_S)
    while not _health_ready(health) and time.monotonic() < deadline:
        time.sleep(1.0)
        health = _probe_health(cfg)
    return health


_HEALTH_TIMEOUT_S = 5
_HEALTH_BODY_BYTES = 200
_HTTP_CHECK_MAX_BYTES = 65_536


def _probe_health(cfg: ProjectConfig) -> dict[str, Any] | None:
    """Fetch the project's health endpoint, if it declared one."""
    if not cfg.health_url:
        return None
    try:
        # noqa justified: the registry restricts health_url to loopback http(s).
        with urllib.request.urlopen(cfg.health_url, timeout=_HEALTH_TIMEOUT_S) as resp:  # noqa: S310
            body = resp.read(_HEALTH_BODY_BYTES).decode("utf-8", "replace")
            return {"url": cfg.health_url, "http_status": resp.status, "body": body}
    except urllib.error.HTTPError as exc:
        # The endpoint replied; the reply is just not a success.
        body = ""
        try:
            body = exc.read(_HEALTH_BODY_BYTES).decode("utf-8", "replace")
        except OSError:
            pass
        return {"url": cfg.health_url, "http_status": exc.code, "body": body}
    except urllib.error.URLError as exc:
        # Nothing answered at all: no status exists to report.
        return {"url": cfg.health_url, "http_status": None, "error": str(exc)}


def _unit_args(unit: str) -> tuple[list[str], dict[str, Any] | None]:
    """Turn an optional unit selector into helper arguments, or a tool error.

    An empty selector means the project's primary unit — the helper resolves it,
    because only the helper knows what the operator declared. A selector that is
    not shaped like a unit name never reaches the socket.
    """
    token = str(unit or "").strip()
    if not token:
        return [], None
    if not _SAFE_UNIT_RE.match(token):
        return [], tool_error(
            "INVALID_ARGUMENT",
            "unit must look like a systemd unit name",
            retryable=False,
        )
    return [token], None


def _undeclared(raw: str) -> dict[str, Any] | None:
    """The helper answers ERR for a unit it was not told about; that is not output."""
    if raw.strip().startswith("ERR "):
        return tool_error(
            "INVALID_ARGUMENT",
            raw.strip(),
            retryable=False,
            next_action="Use a unit declared for this project, or omit unit",
        )
    return None


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
        health = _probe_health(cfg)
        return tool_ok(
            project=project,
            output=raw,
            health=health,
            health_ready=_health_ready(health),
        )

    @server.tool(
        name="service_logs",
        description=(
            "Recent journal lines for a registered project's unit, with secrets redacted "
            "(fixed command via the privileged socket). Optional unit selects one of the "
            "units the operator declared for this project; omit it for the primary. "
            "Cannot target a unit that was not declared, and cannot follow. Read-only."
        ),
        annotations=RO,
    )
    def service_logs(
        project: str,
        unit: str = "",
        lines: int = DEFAULT_LOG_LINES,
    ) -> dict[str, Any]:
        cfg, err = _controllable(project)
        if err is not None or cfg is None:
            return err or {}
        try:
            count = int(lines)
        except (TypeError, ValueError):
            return tool_error("INVALID_ARGUMENT", "lines must be an integer", retryable=False)
        count = max(1, min(count, MAX_LOG_LINES))
        # The helper parses "LOGS [unit] [n]"; a unit name is only honoured when
        # the operator declared it, so an arbitrary one comes back as an error
        # rather than as logs.
        unit_args, err = _unit_args(unit)
        if err is not None:
            return err
        try:
            raw = call_service_ctl(cfg.control_socket or "", "LOGS", args=[*unit_args, str(count)])
        except ServiceCtlError as exc:
            return tool_error(
                "SERVICE_CTL_FAILED",
                str(exc),
                retryable=exc.retryable,
                next_action="Ask an admin to check the project's .socket and .service units",
            )
        if err := _undeclared(raw):
            return err
        # The journal predates any redaction a project added to its own logger,
        # so mask on the way out as well as on the way in.
        clean = redact(raw)
        return tool_ok(
            project=project,
            unit=unit_args[0] if unit_args else "primary",
            lines=count,
            output=clean,
            redactions=redaction_count(raw, clean),
        )

    @server.tool(
        name="http_check",
        description=(
            "Smoke-test a path on the project's declared loopback base URL (preview_url, "
            "else health_url): does it answer, with what status, how fast, how big. "
            "Returns no response body — a page like /token answers 200 by handing out a "
            "credential, and a smoke test has no business carrying that back. Use "
            "service_status for the health body, or browser_open to look at the page. "
            "Read-only."
        ),
        annotations=RO,
    )
    def http_check(project: str, path: str = "/") -> dict[str, Any]:
        cfg = get_project(project)
        if cfg is None:
            return tool_error(
                "UNKNOWN_PROJECT",
                f"unknown project {project!r}; known={list(known_projects())}",
                retryable=False,
            )
        base = cfg.preview_url or cfg.health_url
        if not base:
            return tool_error(
                "INVALID_ARGUMENT",
                f"project {project!r} declares no preview_url or health_url",
                retryable=False,
                next_action="Ask the operator to add preview_url to the registry entry",
            )
        rel = str(path or "/")
        if not rel.startswith("/") or "//" in rel or ".." in rel:
            return tool_error(
                "INVALID_ARGUMENT",
                "path must be absolute and must not traverse",
                retryable=False,
            )
        url = urllib.parse.urljoin(base, rel)
        # urljoin cannot leave the base's host given an absolute path, but the
        # check is cheap and this value is about to be fetched by the server.
        if urllib.parse.urlparse(url).hostname not in _LOOPBACK_HOSTS:
            return tool_error("RISK_BLOCKED", "resolved URL is not loopback", retryable=False)
        started = time.monotonic()
        try:
            # noqa justified: the URL is registry-derived and loopback-checked.
            with urllib.request.urlopen(url, timeout=_HEALTH_TIMEOUT_S) as resp:  # noqa: S310
                body = resp.read(_HTTP_CHECK_MAX_BYTES)
                status = resp.status
                content_type = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            body = b""
            status = exc.code
            content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
        except urllib.error.URLError as exc:
            return tool_error(
                "HTTP_CHECK_FAILED",
                f"{url} did not answer: {exc}",
                retryable=True,
                project=project,
                url=url,
            )
        elapsed_ms = round((time.monotonic() - started) * 1000)
        return tool_ok(
            project=project,
            url=url,
            http_status=status,
            ok_status=200 <= status < 400,
            content_type=content_type,
            bytes_read=len(body),
            elapsed_ms=elapsed_ms,
            note="Body deliberately not returned; a 200 can be a credential.",
        )

    @server.tool(
        name="service_action",
        description=(
            "Run a named maintenance action the operator declared for this project, as "
            "the project account rather than the service account. Call with no action to "
            "list what is available. The command behind each name lives on the host and "
            "cannot be supplied, extended or substituted by the caller — this selects "
            "from a fixed menu, it does not run arbitrary commands as another user. "
            "Use it for work that needs the owning account, such as a check that touches "
            "credentials whose ownership must not change."
        ),
        annotations=DEST,
    )
    def service_action(project: str, action: str = "") -> dict[str, Any]:
        cfg, err = _controllable(project)
        if err is not None or cfg is None:
            return err or {}
        name = str(action or "").strip()
        if name and not _ACTION_RE.match(name):
            return tool_error(
                "INVALID_ARGUMENT",
                "action must match ^[a-z][a-z0-9_-]{0,31}$",
                retryable=False,
            )
        try:
            raw = call_service_ctl(
                cfg.control_socket or "",
                "ACTION",
                args=[name] if name else [],
                timeout_s=_ACTION_TIMEOUT_S,
            )
        except ServiceCtlError as exc:
            return tool_error(
                "SERVICE_CTL_FAILED",
                str(exc),
                retryable=exc.retryable,
                next_action="Check the project control socket and its declared actions",
            )
        if raw.strip().startswith("ERR "):
            return tool_error(
                "INVALID_ARGUMENT",
                raw.strip(),
                retryable=False,
                next_action="Call service_action without action to list what is declared",
            )
        if not name:
            actions = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            return tool_ok(
                project=project,
                actions=actions,
                count=len(actions),
                note="Each name maps to a command written on the host, not here.",
            )
        output, exit_code = split_exit_code(raw)
        return tool_ok(
            project=project,
            action=name,
            exit_code=exit_code,
            output=output,
            stdout_stderr_merged=True,
            note=(
                "exit_code is the action own exit status; a null value means the "
                "helper did not report one and success must not be assumed."
            ),
        )

    @server.tool(
        name="service_restart",
        description=(
            "Restart one of a registered project's units (fixed command via a privileged "
            "socket). Optional unit selects among the units the operator declared for this "
            "project; omit it for the primary. A unit that was not declared is refused, and "
            "this cannot restart the MCP server, the reverse proxy or the host. Returns "
            "status, logs and health."
        ),
        annotations=DEST,
    )
    def service_restart(
        project: str, unit: str = "", wait_for_health_s: float = 0.0
    ) -> dict[str, Any]:
        cfg, err = _controllable(project)
        if err is not None or cfg is None:
            return err or {}
        unit_args, err = _unit_args(unit)
        if err is not None:
            return err
        try:
            raw = call_service_ctl(
                cfg.control_socket or "", "RESTART", args=unit_args, timeout_s=50.0
            )
        except ServiceCtlError as exc:
            return tool_error(
                "SERVICE_CTL_FAILED",
                str(exc),
                retryable=exc.retryable,
                next_action="Inspect the unit's journal as an admin — the MCP cannot elevate",
            )
        if err := _undeclared(raw):
            return err
        restarted = "restart_ok" in raw or "ActiveState=active" in raw
        health = _await_health(cfg, wait_for_health_s)
        payload: dict[str, Any] = {
            "project": project,
            "unit": unit_args[0] if unit_args else "primary",
            "health_ready": _health_ready(health),
            "restart_ok": restarted,
            "output": raw,
            "health": health,
            "note": (
                "restart_ok means the unit came back. health_ready is a separate "
                "question and can be false for a few seconds while a worker "
                "registers; null means this project declares no health endpoint. "
                "Pass wait_for_health_s to poll instead of guessing."
            ),
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
            "Start one of a registered project's units if it is inactive (fixed command). "
            "Optional unit selects among the declared units; omit it for the primary. "
            "Prefer service_restart after changing code."
        ),
        annotations=DEST,
    )
    def service_start(
        project: str, unit: str = "", wait_for_health_s: float = 0.0
    ) -> dict[str, Any]:
        cfg, err = _controllable(project)
        if err is not None or cfg is None:
            return err or {}
        unit_args, err = _unit_args(unit)
        if err is not None:
            return err
        try:
            raw = call_service_ctl(
                cfg.control_socket or "", "START", args=unit_args, timeout_s=50.0
            )
        except ServiceCtlError as exc:
            return tool_error("SERVICE_CTL_FAILED", str(exc), retryable=exc.retryable)
        if err := _undeclared(raw):
            return err
        health = _await_health(cfg, wait_for_health_s)
        return tool_ok(
            project=project,
            unit=unit_args[0] if unit_args else "primary",
            output=raw,
            health=health,
            health_ready=_health_ready(health),
        )
