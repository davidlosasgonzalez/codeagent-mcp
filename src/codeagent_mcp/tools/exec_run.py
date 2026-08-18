"""MCP tool: exec_run."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from codeagent_mcp.errors import tool_error, tool_ok
from codeagent_mcp.exec.env import (
    apply_git_safe_directory,
    apply_project_env,
    merge_env_overrides,
)
from codeagent_mcp.exec.gate import get_exec_gate
from codeagent_mcp.exec.runas import discard, split_exit_code, write_spec
from codeagent_mcp.exec.runner import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_S,
    HARD_MAX_OUTPUT_BYTES,
    HARD_MAX_TIMEOUT_S,
    run_argv,
)
from codeagent_mcp.paths import resolve_under_root
from codeagent_mcp.service_ctl import ServiceCtlError, call_service_ctl
from codeagent_mcp.tools.annotations import EXEC
from codeagent_mcp.tools.workspace import get_lease_manager
from codeagent_mcp.workspace.projects import get_project


def _run_as_project_account(
    *,
    command: list[str],
    cwd_path: Path,
    timeout_s: float,
    max_output_bytes: int,
    run_as: str,
    project_cfg: Any,
    lease_id: str,
) -> dict[str, Any]:
    """Run the command as the account the operator declared for this project.

    The server cannot drop to another user itself: its unit sets
    NoNewPrivileges, which closes sudo and every setuid path. So the crossing
    happens in the one place that is already privileged and already audited, the
    project control socket, and that helper re-checks both the account and the
    working directory rather than trusting the checks made here.
    """
    if project_cfg is None or not getattr(project_cfg, "run_as_user", None):
        return tool_error(
            "RISK_BLOCKED",
            "this project declares no run_as_user",
            retryable=False,
            next_action="Ask the operator to add run_as_user to the registry entry",
        )
    if run_as != project_cfg.run_as_user:
        return tool_error(
            "RISK_BLOCKED",
            f"run_as must be {project_cfg.run_as_user!r} for this project",
            retryable=False,
            next_action="Omit run_as to run as the service account",
        )
    if not project_cfg.control_socket:
        return tool_error(
            "INVALID_ARGUMENT",
            "run_as needs the project control socket, which is not declared",
            retryable=False,
        )

    token = write_spec(argv=list(command), cwd=str(cwd_path), timeout_s=int(timeout_s))
    try:
        raw = call_service_ctl(
            project_cfg.control_socket,
            "RUNAS",
            args=[token],
            timeout_s=timeout_s + 15.0,
        )
    except ServiceCtlError as exc:
        discard(token)
        return tool_error(
            "SERVICE_CTL_FAILED",
            str(exc),
            retryable=exc.retryable,
            next_action="Check the project control socket and its runas configuration",
        )
    if raw.strip().startswith("ERR "):
        discard(token)
        return tool_error("RISK_BLOCKED", raw.strip(), retryable=False)

    output, exit_code = split_exit_code(raw)
    truncated = False
    if len(output.encode("utf-8")) > max_output_bytes:
        output = output.encode("utf-8")[:max_output_bytes].decode("utf-8", "ignore")
        truncated = True
    return tool_ok(
        project=project_cfg.name,
        ran_as=run_as,
        cwd=str(cwd_path),
        exit_code=exit_code,
        # The helper merges the two streams onto one socket, so there is no
        # honest way to split them back apart here.
        output=output,
        stdout_stderr_merged=True,
        truncated=truncated,
        lease_id_present=bool(lease_id),
        note=(
            "Ran through the project privileged helper as its declared account. "
            "exit_code is the command own exit status; a null value means the "
            "helper did not report one and success must not be assumed."
        ),
    )


def register_exec_tools(server: FastMCP) -> None:
    @server.tool(
        name="exec_run",
        description=(
            "Run a non-interactive command as an argv list (no shell). "
            "Set run_as to the account this project declares to run as that user "
            "(never root) through its privileged helper; stdout and stderr come "
            "back merged. "
            "Requires a valid exclusive lease_id from workspace_acquire. "
            "cwd must stay under the project root. "
            "Use for pytest/builds/scripts; use terminal_* later for interactive PTY. "
            "Requires lease; production checkout write gated by CODEAGENT_*_WRITE + project policy."
        ),
        annotations=EXEC,
    )
    def exec_run(
        lease_id: str,
        command: list[str],
        cwd: str | None = None,
        run_as: str = "",
        env_overrides: dict[str, str] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> dict[str, Any]:
        return run_exec_run(
            lease_id=lease_id,
            command=command,
            cwd=cwd,
            run_as=run_as,
            env_overrides=env_overrides,
            timeout_s=timeout_s,
            max_output_bytes=max_output_bytes,
        )


def run_exec_run(
    *,
    lease_id: str,
    command: list[str],
    cwd: str | None = None,
    run_as: str = "",
    env_overrides: dict[str, str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> dict[str, Any]:
    """Core implementation (also used by tests without FastMCP)."""
    if not lease_id or not str(lease_id).strip():
        return tool_error(
            "LEASE_REQUIRED",
            "lease_id is required for exec_run",
            retryable=False,
            next_action="Call workspace_acquire first and pass lease_id",
        )
    if not isinstance(command, list) or not command:
        return tool_error(
            "INVALID_ARGUMENT",
            "command must be a non-empty argv list of strings (no shell)",
            retryable=False,
        )
    if not all(isinstance(part, str) and part for part in command):
        return tool_error(
            "INVALID_ARGUMENT",
            "command argv entries must be non-empty strings",
            retryable=False,
        )
    try:
        timeout_val = float(timeout_s)
    except (TypeError, ValueError):
        return tool_error("INVALID_ARGUMENT", "timeout_s must be a number", retryable=False)
    if timeout_val <= 0 or timeout_val > HARD_MAX_TIMEOUT_S:
        return tool_error(
            "INVALID_ARGUMENT",
            f"timeout_s must be in (0, {HARD_MAX_TIMEOUT_S}]",
            retryable=False,
        )
    try:
        max_out = int(max_output_bytes)
    except (TypeError, ValueError):
        return tool_error(
            "INVALID_ARGUMENT", "max_output_bytes must be an integer", retryable=False
        )
    if max_out < 1 or max_out > HARD_MAX_OUTPUT_BYTES:
        return tool_error(
            "INVALID_ARGUMENT",
            f"max_output_bytes must be in [1, {HARD_MAX_OUTPUT_BYTES}]",
            retryable=False,
        )

    lease = get_lease_manager().require_active(lease_id=str(lease_id).strip())
    if lease.get("ok") is not True:
        return lease

    root = lease["root"]
    try:
        cwd_path = resolve_under_root(cwd if cwd is not None else root, root)
    except ValueError as exc:
        return tool_error(
            "PATH_OUTSIDE_ROOT",
            str(exc),
            retryable=False,
            next_action="Use a cwd under the project root returned by workspace_acquire",
        )
    if not cwd_path.exists():
        return tool_error(
            "NOT_FOUND",
            f"cwd does not exist: {cwd_path}",
            retryable=False,
        )
    if not cwd_path.is_dir():
        return tool_error(
            "INVALID_ARGUMENT",
            f"cwd is not a directory: {cwd_path}",
            retryable=False,
        )

    env = merge_env_overrides(env_overrides)
    if isinstance(env, dict) and env.get("ok") is False:
        return env

    project_cfg = get_project(str(lease["project"]))
    if project_cfg is not None and project_cfg.env:
        env = apply_project_env(env, project_cfg.env)  # type: ignore[arg-type]
    env = apply_git_safe_directory(env, root)  # type: ignore[arg-type]

    if str(run_as).strip():
        return _run_as_project_account(
            command=command,
            cwd_path=cwd_path,
            timeout_s=timeout_val,
            max_output_bytes=max_out,
            run_as=str(run_as).strip(),
            project_cfg=project_cfg,
            lease_id=str(lease_id).strip(),
        )

    gate = get_exec_gate()
    lid = str(lease_id).strip()
    if not gate.try_enter(lid, command=" ".join(command[:3])):
        holder = gate.holder(lid)
        held = f" (holding {holder.command!r} for {holder.age_s():.0f}s)" if holder else ""
        return tool_error(
            "PROCESS_RUNNING",
            f"another exec_run is already active for this lease_id{held}",
            retryable=True,
            next_action=(
                "Wait for the in-flight exec_run to finish; "
                "if nothing is running, ops_cleanup releases a stale gate entry"
            ),
        )
    try:
        try:
            result = run_argv(
                command=command,
                cwd=cwd_path,
                env=env,  # type: ignore[arg-type]
                timeout_s=timeout_val,
                max_output_bytes=max_out,
            )
        except FileNotFoundError:
            return tool_error(
                "NOT_FOUND",
                f"executable not found: {command[0]!r}",
                retryable=False,
                next_action="Check argv[0] exists on PATH for the service user",
            )
        except OSError as exc:
            return tool_error(
                "INTERNAL_ERROR",
                f"failed to start process: {exc}",
                retryable=True,
            )
    finally:
        gate.exit(lid)

    # Renew again after successful start/finish path (activity).
    get_lease_manager().require_active(lease_id=lid)

    payload: dict[str, Any] = tool_ok(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        duration_ms=result.duration_ms,
        cwd=result.cwd,
        stdout_truncated=result.stdout_truncated,
        stderr_truncated=result.stderr_truncated,
        signal=result.signal_name,
        output_incomplete=result.output_incomplete,
        command=result.command,
        lease_id=lid,
        project=lease["project"],
    )
    if result.timed_out:
        payload["timeout_s"] = timeout_val
    return payload
