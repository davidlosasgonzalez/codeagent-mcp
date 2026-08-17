"""A pane shell must actually hold the tmpdir that terminal_create reports.

Regression: the tool reported `/var/lib/codeagent-mcp/tmp` while `$TMPDIR` in the
pane was empty. `tmux set-environment` was being handed `NAME=value` as a single
argument, which it rejects, and the call tolerated failure — so the sync was a
silent no-op and only the *reported* value was ever right.

These tests read the environment back out of a real pane, because that is the
only place the claim can be checked.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from codeagent_mcp.terminal import tmux
from codeagent_mcp.terminal.service import TerminalService
from codeagent_mcp.terminal.store import TerminalStore
from codeagent_mcp.tools.terminal import set_terminal_service
from codeagent_mcp.tools.workspace import set_lease_manager
from codeagent_mcp.workspace.lease_store import LeaseStore
from codeagent_mcp.workspace.leases import LeaseManager

PANE_SETTLE_S = 2.0
POLL_INTERVAL_S = 0.1


@pytest.fixture()
def terminal_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A private tmux socket plus a private temp root, so nothing touches the host's."""
    private_tmp = tmp_path / "svc-tmp"
    monkeypatch.setenv("TMPDIR", str(private_tmp))
    monkeypatch.setenv("CODEAGENT_TMUX_SOCKET", str(tmp_path / "tmux.sock"))
    monkeypatch.setenv("CODEAGENT_TMUX_CONF", str(tmp_path / "tmux.conf"))
    monkeypatch.setenv("CODEAGENT_TMUX_TMPDIR", str(tmp_path / "tmux"))
    monkeypatch.setenv("CODEAGENT_TERMINAL_STORE", str(tmp_path / "terminals.json"))
    monkeypatch.setenv("CODEAGENT_SPOOL_ROOT", str(tmp_path / "spool"))
    monkeypatch.setenv("CODEAGENT_LEASE_STORE", str(tmp_path / "leases.json"))
    monkeypatch.setenv("CODEAGENT_LEASE_TTL_S", "2700")
    runtime = Path(f"/run/user/{os.getuid()}")
    if runtime.is_dir():
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))

    from conftest import override_projects

    from codeagent_mcp.workspace import projects as projects_mod

    root = tmp_path / "proj"
    root.mkdir()
    override_projects(
        monkeypatch,
        {"demo": projects_mod.ProjectConfig(name="demo", root=str(root))},
    )

    mgr = LeaseManager(LeaseStore(tmp_path / "leases.json"), ttl_s=2700)
    set_lease_manager(mgr)
    svc = TerminalService(TerminalStore(tmp_path / "terminals.json"))
    set_terminal_service(svc)

    yield svc, mgr, str(private_tmp)

    tmux.run_tmux(["kill-server"], check=False)
    set_terminal_service(None)
    set_lease_manager(None)


def _global_env(prefix: str) -> list[str]:
    proc = tmux.run_tmux(["show-environment", "-g"], check=False)
    return [line for line in (proc.stdout or "").splitlines() if line.startswith(prefix)]


def _echo_in_pane(pane_id: str, variable: str) -> str:
    """Run `echo MARK=[$VAR]` in the pane and return what it printed."""
    tmux.run_tmux(["send-keys", "-t", pane_id, f"echo MARK=[${variable}]", "Enter"])
    deadline = time.monotonic() + PANE_SETTLE_S
    while time.monotonic() < deadline:
        cap = tmux.run_tmux(["capture-pane", "-p", "-t", pane_id], check=False)
        for line in (cap.stdout or "").splitlines():
            if line.startswith("MARK=[") and line.endswith("]"):
                return line[len("MARK=[") : -1]
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"pane {pane_id} never echoed {variable}")


def _start_server_without_tmpdir() -> None:
    """Start the tmux server the way the broken host had it: no TMPDIR inherited.

    A server started *with* TMPDIR copies it into the global environment on its
    own, which hides a broken sync entirely — that is why this went unnoticed for
    so long, and why every assertion below has to start from a stale server.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/root"),
        "TMUX_TMPDIR": str(tmux.tmux_tmpdir()),
    }
    tmux.ensure_runtime_dirs()
    subprocess.run(
        tmux.tmux_argv(
            "new-session", "-d", "-s", tmux.SESSION_NAME, "-n", "_boot", "--", "sleep", "infinity"
        ),
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert _global_env("TMPDIR=") == [], "precondition: the stale server must lack TMPDIR"


def test_sync_repairs_a_server_that_started_without_tmpdir(terminal_env) -> None:
    """The exact bug: `set-environment -g NAME=value` is rejected by tmux."""
    _svc, _mgr, private_tmp = terminal_env
    _start_server_without_tmpdir()

    tmux.sync_tmpdir_to_server()

    assert f"TMPDIR={private_tmp}" in _global_env("TMPDIR=")


def test_sync_covers_all_three_temp_variables(terminal_env) -> None:
    _svc, _mgr, private_tmp = terminal_env
    _start_server_without_tmpdir()

    tmux.sync_tmpdir_to_server()

    for key in ("TMPDIR", "TEMP", "TMP"):
        assert f"{key}={private_tmp}" in _global_env(f"{key}=")


def test_pane_on_a_stale_server_still_gets_tmpdir(terminal_env) -> None:
    """End to end against the host's actual failure: stale server, new pane."""
    svc, mgr, private_tmp = terminal_env
    _start_server_without_tmpdir()

    lease = mgr.acquire(project="demo")
    created = svc.create(lease_id=lease["lease_id"], alias="main")
    assert created["ok"] is True
    assert created["tmpdir"] == private_tmp
    assert _echo_in_pane(created["pane_id"], "TMPDIR") == created["tmpdir"]


def test_pane_shell_holds_the_reported_tmpdir(terminal_env) -> None:
    """End to end: what terminal_create reports is what the shell has."""
    svc, mgr, private_tmp = terminal_env
    lease = mgr.acquire(project="demo")
    created = svc.create(lease_id=lease["lease_id"], alias="main")
    assert created["ok"] is True
    assert created["tmpdir"] == private_tmp
    assert _echo_in_pane(created["pane_id"], "TMPDIR") == created["tmpdir"]


def test_pane_is_correct_even_when_the_global_environment_is_stale(terminal_env) -> None:
    """`new-window -e` is the belt: a pane must not depend on server-wide state."""
    svc, mgr, private_tmp = terminal_env
    tmux.ensure_server()
    for key in ("TMPDIR", "TEMP", "TMP"):
        tmux.run_tmux(["set-environment", "-gu", key], check=False)

    lease = mgr.acquire(project="demo")
    created = svc.create(lease_id=lease["lease_id"], alias="app")
    assert created["ok"] is True
    assert _echo_in_pane(created["pane_id"], "TMPDIR") == private_tmp


def test_temp_aliases_reach_the_pane_too(terminal_env) -> None:
    svc, mgr, private_tmp = terminal_env
    lease = mgr.acquire(project="demo")
    created = svc.create(lease_id=lease["lease_id"], alias="debug")
    for variable in ("TEMP", "TMP"):
        assert _echo_in_pane(created["pane_id"], variable) == private_tmp
