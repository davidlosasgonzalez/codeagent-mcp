"""Argv subprocess runner: no shell, process-group kill, output caps."""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from codeagent_mcp.exec.env import child_umask

DEFAULT_MAX_OUTPUT_BYTES = 200_000
DEFAULT_TIMEOUT_S = 120
HARD_MAX_TIMEOUT_S = 3_600
HARD_MAX_OUTPUT_BYTES = 2_000_000


@dataclass(frozen=True, slots=True)
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    cwd: str
    stdout_truncated: bool
    stderr_truncated: bool
    signal_name: str | None
    command: list[str]
    output_incomplete: bool = False


def _decode_cap(data: bytes, limit: int) -> tuple[str, bool]:
    truncated = len(data) > limit
    chunk = data[:limit] if truncated else data
    return chunk.decode("utf-8", errors="replace"), truncated


# How long to wait for pipes to drain after killing. Bounded on purpose: an
# unbounded wait here is what leaked a worker thread and, with it, the exec gate.
DRAIN_TIMEOUT_S = 5.0


def _kill_session(pid: int) -> None:
    """Kill anything still in the child's session.

    start_new_session makes the child a session leader, so its descendants share
    its session id even when they give themselves a new process group to escape
    killpg. Reading sid from /proc is cheap and needs no extra privilege.
    """
    try:
        entries = list(pathlib.Path("/proc").iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.name.isdigit():
            continue
        victim = int(entry.name)
        if victim == pid:
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Fields after the (possibly parenthesised, possibly spaced) comm.
        tail = stat.rpartition(")")[2].split()
        if len(tail) < 4:
            continue
        try:
            session = int(tail[3])
        except ValueError:
            continue
        if session != pid:
            continue
        try:
            os.kill(victim, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            continue


def _abandon_pipes(proc: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    """Close the pipes and return whatever is already buffered.

    Closing is what unblocks the writer; not closing is what would leave a file
    descriptor and a thread behind on every timeout.
    """
    out = b""
    err = b""
    for stream, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
        if stream is None:
            continue
        try:
            os.set_blocking(stream.fileno(), False)
            data = stream.read() or b""
        except (OSError, ValueError):
            data = b""
        if name == "stdout":
            out = data
        else:
            err = data
        try:
            stream.close()
        except OSError:
            pass
    return out, err


def run_argv(
    *,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
    max_output_bytes: int,
) -> ExecResult:
    """Run ``command`` without a shell; kill the process group on timeout."""
    start = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603 — argv list, shell=False
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        shell=False,
        umask=child_umask(),
    )
    timed_out = False
    output_incomplete = False
    signal_name: str | None = None
    try:
        stdout_b, stderr_b = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        signal_name = "SIGKILL"
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            stdout_b, stderr_b = proc.communicate(timeout=DRAIN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            _kill_session(proc.pid)
            try:
                stdout_b, stderr_b = proc.communicate(timeout=DRAIN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                # Something outside the process group still holds the pipes.
                # Waiting longer would block this thread for the life of the
                # process, so stop reading and say the output is incomplete.
                output_incomplete = True
                stdout_b, stderr_b = _abandon_pipes(proc)
    duration_ms = int((time.monotonic() - start) * 1000)
    exit_code = proc.returncode
    if timed_out and exit_code is None:
        exit_code = -9
    # Negative returncode from Python means killed by signal (-N)
    if exit_code is not None and exit_code < 0 and signal_name is None:
        try:
            signal_name = signal.Signals(-exit_code).name
        except (ValueError, SystemError):
            signal_name = f"signal_{-exit_code}"
    stdout, stdout_truncated = _decode_cap(stdout_b or b"", max_output_bytes)
    stderr, stderr_truncated = _decode_cap(stderr_b or b"", max_output_bytes)
    return ExecResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        timed_out=timed_out,
        duration_ms=duration_ms,
        cwd=str(cwd),
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        signal_name=signal_name,
        command=list(command),
        output_incomplete=output_incomplete,
    )
