"""Low-level argv-only tmux client for a dedicated socket."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from codeagent_mcp.exec.env import child_umask, ensure_service_tmpdir

log = logging.getLogger(__name__)

DEFAULT_SOCKET = "/var/lib/codeagent-mcp/tmux/default.sock"
DEFAULT_CONF = "/var/lib/codeagent-mcp/tmux/tmux.conf"
DEFAULT_TMPDIR = "/var/lib/codeagent-mcp/tmux"
DEFAULT_SHELL = ("/bin/bash", "--norc", "--noprofile")
SESSION_NAME = "codeagent"

TMUX_CONF_BODY = """set -g exit-empty off
set -g exit-unattached off
set -g status off
set -g history-limit 5000
"""


class TmuxError(RuntimeError):
    def __init__(self, message: str, *, stderr: str = "", returncode: int = 1) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


@dataclass(frozen=True)
class PaneInfo:
    session_name: str
    window_name: str
    pane_id: str
    pane_pid: int
    pane_dead: bool
    pane_current_command: str
    pane_current_path: str


def socket_path() -> Path:
    return Path(os.environ.get("CODEAGENT_TMUX_SOCKET", DEFAULT_SOCKET))


def conf_path() -> Path:
    return Path(os.environ.get("CODEAGENT_TMUX_CONF", DEFAULT_CONF))


def tmux_tmpdir() -> Path:
    return Path(os.environ.get("CODEAGENT_TMUX_TMPDIR", DEFAULT_TMPDIR))


def ensure_runtime_dirs() -> None:
    sock = socket_path()
    tmp = tmux_tmpdir()
    conf = conf_path()
    tmp.mkdir(parents=True, mode=0o700, exist_ok=True)
    sock.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not conf.exists():
        conf.write_text(TMUX_CONF_BODY, encoding="utf-8")
        conf.chmod(0o600)


def codeagent_tmpdir() -> Path:
    """The same private temp root exec_run pins, created if absent.

    Delegated so a pane shell and a child of ``exec_run`` cannot drift apart:
    a tool that writes a temp file in one and reads it in the other has to find
    the same directory.
    """
    return ensure_service_tmpdir()


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["TMUX_TMPDIR"] = str(tmux_tmpdir())
    tmp = str(codeagent_tmpdir())
    env["TMPDIR"] = tmp
    env["TEMP"] = tmp
    env["TMP"] = tmp
    runtime = Path(f"/run/user/{os.getuid()}")
    if runtime.is_dir():
        env.setdefault("XDG_RUNTIME_DIR", str(runtime))
    return env


def tmpdir_env_pairs() -> list[tuple[str, str]]:
    """The temp variables every pane shell must start with."""
    tmp = str(codeagent_tmpdir())
    return [(key, tmp) for key in ("TMPDIR", "TEMP", "TMP")]


def sync_tmpdir_to_server() -> None:
    """Push TMPDIR into the durable tmux server env (survives MCP restarts).

    ``set-environment`` takes the name and the value as **two arguments**. Passing
    ``NAME=value`` as one is rejected with "variable name contains =", and because
    the call tolerates failure the whole sync was a silent no-op: terminal_create
    reported a tmpdir that the pane shell did not actually have. Hence the log
    line — a sync that stops working must say so.
    """
    if not server_alive():
        return
    for key, value in tmpdir_env_pairs():
        proc = run_tmux(["set-environment", "-g", key, value], check=False)
        if proc.returncode != 0:
            log.warning(
                "tmux set-environment -g %s failed (rc=%s): %s",
                key,
                proc.returncode,
                (proc.stderr or "").strip(),
            )


def tmux_argv(*args: str) -> list[str]:
    return ["tmux", "-S", str(socket_path()), "-f", str(conf_path()), *args]


def run_tmux(
    args: Sequence[str],
    *,
    check: bool = True,
    timeout_s: float = 10.0,
) -> subprocess.CompletedProcess[str]:
    ensure_runtime_dirs()
    cmd = tmux_argv(*args)
    try:
        proc = subprocess.run(
            list(cmd),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=_env(),
            # The first client starts the tmux server, which passes its umask on
            # to every pane shell; set it here so panes match exec_run.
            umask=child_umask(),
        )
    except subprocess.TimeoutExpired as exc:
        raise TmuxError(f"tmux timed out: {' '.join(cmd)}") from exc
    if check and proc.returncode != 0:
        raise TmuxError(
            f"tmux failed ({proc.returncode}): {' '.join(cmd)}",
            stderr=(proc.stderr or "").strip(),
            returncode=proc.returncode,
        )
    return proc


def server_alive() -> bool:
    proc = run_tmux(["list-sessions"], check=False)
    if proc.returncode == 0:
        return True
    err = ((proc.stderr or "") + (proc.stdout or "")).lower()
    return "no server running" not in err and "error connecting" not in err and proc.returncode == 0


def ensure_server() -> None:
    """Ensure dedicated tmux server exists (lazy). Survives MCP restart."""
    if server_alive():
        sync_tmpdir_to_server()
        return
    sock = socket_path()
    # Only remove a *stale* socket. False-negative list-sessions must not
    # orphan a live server (Gate J).
    if sock.exists():
        probe = run_tmux(["list-sessions"], check=False)
        err = ((probe.stderr or "") + (probe.stdout or "")).lower()
        stale = probe.returncode != 0 and (
            "no server running" in err or "error connecting" in err or "no such file" in err
        )
        if stale:
            try:
                sock.unlink()
            except OSError:
                pass
        elif probe.returncode == 0:
            return
        else:
            raise TmuxError(
                "tmux socket exists but list-sessions failed; refusing to recreate",
                stderr=(probe.stderr or "").strip(),
                returncode=probe.returncode,
            )
    run_tmux(
        [
            "new-session",
            "-d",
            "-s",
            SESSION_NAME,
            "-n",
            "_boot",
            "--",
            "sleep",
            "infinity",
        ]
    )
    sync_tmpdir_to_server()


def list_panes() -> list[PaneInfo]:
    if not server_alive():
        return []
    fmt = (
        "#{session_name}\t#{window_name}\t#{pane_id}\t#{pane_pid}\t"
        "#{pane_dead}\t#{pane_current_command}\t#{pane_current_path}"
    )
    proc = run_tmux(["list-panes", "-a", "-F", fmt], check=False)
    if proc.returncode != 0:
        return []
    out: list[PaneInfo] = []
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        out.append(
            PaneInfo(
                session_name=parts[0],
                window_name=parts[1],
                pane_id=parts[2],
                pane_pid=int(parts[3] or "0"),
                pane_dead=parts[4] == "1",
                pane_current_command=parts[5],
                pane_current_path=parts[6],
            )
        )
    return out


def get_pane(pane_id: str) -> PaneInfo | None:
    for pane in list_panes():
        if pane.pane_id == pane_id:
            return pane
    return None


def create_window(*, alias: str, cwd: str, shell: Sequence[str] = DEFAULT_SHELL) -> PaneInfo:
    ensure_server()
    sync_tmpdir_to_server()
    # -e sets the variable on this pane directly, so a pane is correct even when
    # the server's global environment is stale or was never synced. Unlike
    # set-environment, new-window -e wants a single KEY=VALUE argument.
    env_args: list[str] = []
    for key, value in tmpdir_env_pairs():
        env_args.extend(["-e", f"{key}={value}"])
    proc = run_tmux(
        [
            "new-window",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            SESSION_NAME,
            "-n",
            alias,
            "-c",
            cwd,
            *env_args,
            "--",
            *shell,
        ]
    )
    pane_id = (proc.stdout or "").strip().splitlines()[0].strip()
    if not pane_id.startswith("%"):
        raise TmuxError(f"unexpected pane_id from new-window: {pane_id!r}")
    info = get_pane(pane_id)
    if info is None:
        return PaneInfo(
            session_name=SESSION_NAME,
            window_name=alias,
            pane_id=pane_id,
            pane_pid=0,
            pane_dead=False,
            pane_current_command="bash",
            pane_current_path=cwd,
        )
    return info


def kill_pane(pane_id: str) -> None:
    run_tmux(["kill-pane", "-t", pane_id], check=False)


def send_literal(pane_id: str, text: str) -> None:
    # -- prevents text starting with - from being parsed as flags
    run_tmux(["send-keys", "-t", pane_id, "-l", "--", text])


def send_key(pane_id: str, key: str) -> None:
    run_tmux(["send-keys", "-t", pane_id, "--", key])


def pane_pipe_active(pane_id: str) -> bool:
    """True if tmux reports an active pipe-pane on this pane."""
    proc = run_tmux(["display-message", "-p", "-t", pane_id, "#{pane_pipe}"], check=False)
    if proc.returncode != 0:
        return False
    return (proc.stdout or "").strip() == "1"


def pipe_pane_attach(pane_id: str, spool_path: Path) -> None:
    """Attach/replace pipe-pane -O appending to spool_path."""
    import shlex

    path = Path(spool_path).resolve()
    cmd = f"cat >> {shlex.quote(str(path))}"
    run_tmux(["pipe-pane", "-O", "-t", pane_id, cmd])


def pipe_pane_detach(pane_id: str) -> None:
    run_tmux(["pipe-pane", "-t", pane_id], check=False)


def capture_pane_snapshot(pane_id: str, *, include_history: bool = True) -> str:
    """Visible/scrollback photo via capture-pane — not an incremental log."""
    args = ["capture-pane", "-p", "-J", "-t", pane_id]
    if include_history:
        args = ["capture-pane", "-p", "-J", "-S", "-", "-t", pane_id]
    proc = run_tmux(args)
    return proc.stdout or ""


def capture_pane(pane_id: str) -> str:
    """Test oracle only — not an MCP tool."""
    proc = run_tmux(["capture-pane", "-p", "-J", "-t", pane_id])
    return proc.stdout or ""
