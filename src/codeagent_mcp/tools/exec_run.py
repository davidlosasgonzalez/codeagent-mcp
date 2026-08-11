"""MCP tool: exec_run."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from codeagent_mcp.errors import tool_error, tool_ok
from codeagent_mcp.exec.env import apply_project_env, merge_env_overrides
from codeagent_mcp.exec.gate import get_exec_gate
from codeagent_mcp.exec.runner import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_S,
    HARD_MAX_OUTPUT_BYTES,
    HARD_MAX_TIMEOUT_S,
    run_argv,
)
from codeagent_mcp.paths import resolve_under_root
from codeagent_mcp.tools.annotations import EXEC
from codeagent_mcp.tools.workspace import get_lease_manager
from codeagent_mcp.workspace.projects import get_project


def register_exec_tools(server: FastMCP) -> None:
    @server.tool(
        name="exec_run",
        description=(
            "Run a non-interactive command as an argv list (no shell). "
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
        env_overrides: dict[str, str] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> dict[str, Any]:
        return run_exec_run(
            lease_id=lease_id,
            command=command,
            cwd=cwd,
            env_overrides=env_overrides,
            timeout_s=timeout_s,
            max_output_bytes=max_output_bytes,
        )


def run_exec_run(
    *,
    lease_id: str,
    command: list[str],
    cwd: str | None = None,
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

    gate = get_exec_gate()
    lid = str(lease_id).strip()
    if not gate.try_enter(lid):
        return tool_error(
            "PROCESS_RUNNING",
            "another exec_run is already active for this lease_id",
            retryable=True,
            next_action="Wait for the in-flight exec_run to finish",
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
        command=result.command,
        lease_id=lid,
        project=lease["project"],
    )
    if result.timed_out:
        payload["timeout_s"] = timeout_val
    return payload
