"""Terminal lifecycle tests (dedicated tmux socket)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codeagent_mcp.terminal import tmux
from codeagent_mcp.terminal.service import TerminalService, poll_until
from codeagent_mcp.terminal.store import TerminalStore
from codeagent_mcp.tools.terminal import set_terminal_service
from codeagent_mcp.tools.workspace import set_lease_manager
from codeagent_mcp.workspace.lease_store import LeaseStore
from codeagent_mcp.workspace.leases import LeaseManager


@pytest.fixture()
def terminal_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sock = tmp_path / "tmux.sock"
    conf = tmp_path / "tmux.conf"
    store = tmp_path / "terminals.json"
    lease_path = tmp_path / "leases.json"
    monkeypatch.setenv("CODEAGENT_TMUX_SOCKET", str(sock))
    monkeypatch.setenv("CODEAGENT_TMUX_CONF", str(conf))
    monkeypatch.setenv("CODEAGENT_TMUX_TMPDIR", str(tmp_path / "tmux"))
    monkeypatch.setenv("CODEAGENT_TERMINAL_STORE", str(store))
    monkeypatch.setenv("CODEAGENT_SPOOL_ROOT", str(tmp_path / "spool"))
    monkeypatch.setenv("CODEAGENT_LEASE_STORE", str(lease_path))
    monkeypatch.setenv("CODEAGENT_LEASE_TTL_S", "2700")
    # Prefer real runtime dir when available
    runtime = Path(f"/run/user/{os.getuid()}")
    if runtime.is_dir():
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))

    mgr = LeaseManager(LeaseStore(lease_path), ttl_s=2700)
    set_lease_manager(mgr)
    svc = TerminalService(TerminalStore(store))
    set_terminal_service(svc)

    # project root for cwd confinement — use tmp as fake root via monkeypatch projects
    from conftest import override_projects

    from codeagent_mcp.workspace import projects as projects_mod

    (tmp_path / "proj").mkdir()
    override_projects(
        monkeypatch,
        {"demo": projects_mod.ProjectConfig(name="demo", root=str(tmp_path / "proj"))},
    )

    yield svc, mgr, tmp_path

    # cleanup tmux server for this socket
    try:
        tmux.run_tmux(["kill-server"], check=False)
    except Exception:
        pass
    set_terminal_service(None)
    set_lease_manager(None)


def _pane_cmd(pane_id: str) -> str:
    pane = tmux.get_pane(pane_id)
    return "" if pane is None else pane.pane_current_command


def _acquire(mgr: LeaseManager) -> str:
    acq = mgr.acquire(project="demo")
    assert acq["ok"] is True
    return acq["lease_id"]


def test_create_list_write_key_bash(terminal_env) -> None:
    svc, mgr, tmp = terminal_env
    lid = _acquire(mgr)
    created = svc.create(lease_id=lid, alias="main")
    assert created["ok"] is True, created
    pane_id = created["pane_id"]
    assert pane_id.startswith("%")

    listed = svc.list(lease_id=lid)
    assert listed["ok"] and listed["count"] == 1
    assert listed["terminals"][0]["alias"] == "main"

    st = svc.status(lease_id=lid, alias="main")
    assert st["ok"] and st["alive"] is True

    marker = "TERMINAL_MARKER_42"
    wr = svc.write(lease_id=lid, alias="main", text=f"echo {marker}")
    assert wr["ok"]
    assert svc.key(lease_id=lid, alias="main", key="ENTER")["ok"]

    assert poll_until(
        lambda: marker in tmux.capture_pane(pane_id),
        timeout_s=5.0,
    ), tmux.capture_pane(pane_id)


def test_interrupt_foreground_sleep(terminal_env) -> None:
    svc, mgr, _ = terminal_env
    lid = _acquire(mgr)
    created = svc.create(lease_id=lid, alias="app")
    assert created["ok"]
    pane_id = created["pane_id"]

    assert svc.write(lease_id=lid, alias="app", text="sleep 60")["ok"]
    assert svc.key(lease_id=lid, alias="app", key="ENTER")["ok"]
    assert poll_until(
        lambda: _pane_cmd(pane_id) == "sleep",
        timeout_s=5.0,
    )

    assert svc.interrupt(lease_id=lid, alias="app")["ok"]
    assert poll_until(
        lambda: _pane_cmd(pane_id) == "bash",
        timeout_s=5.0,
    )


def test_python_repl_enter(terminal_env) -> None:
    svc, mgr, _ = terminal_env
    lid = _acquire(mgr)
    created = svc.create(lease_id=lid, alias="debug")
    assert created["ok"]
    pane_id = created["pane_id"]
    assert svc.write(lease_id=lid, alias="debug", text="python3 -q")["ok"]
    assert svc.key(lease_id=lid, alias="debug", key="ENTER")["ok"]
    assert poll_until(
        lambda: _pane_cmd(pane_id) in {"python3", "python"},
        timeout_s=5.0,
    )
    assert svc.write(lease_id=lid, alias="debug", text="print(12321)")["ok"]
    assert svc.key(lease_id=lid, alias="debug", key="ENTER")["ok"]
    assert poll_until(lambda: "12321" in tmux.capture_pane(pane_id), timeout_s=5.0)
    assert svc.key(lease_id=lid, alias="debug", key="CTRL_D")["ok"]


def test_multi_terminals_and_alias_conflict(terminal_env) -> None:
    svc, mgr, _ = terminal_env
    lid = _acquire(mgr)
    assert svc.create(lease_id=lid, alias="main")["ok"]
    assert svc.create(lease_id=lid, alias="app")["ok"]
    assert svc.create(lease_id=lid, alias="debug")["ok"]
    fourth = svc.create(lease_id=lid, alias="extra")
    assert fourth["ok"] is False
    assert fourth["error"]["code"] == "RISK_BLOCKED"
    conflict = svc.create(lease_id=lid, alias="main")
    assert conflict["error"]["code"] == "CONFLICT"


def test_dead_shell_session_dead_and_reset(terminal_env) -> None:
    svc, mgr, _ = terminal_env
    lid = _acquire(mgr)
    created = svc.create(lease_id=lid, alias="main")
    assert created["ok"]
    pane_id = created["pane_id"]
    assert svc.write(lease_id=lid, alias="main", text="exit")["ok"]
    assert svc.key(lease_id=lid, alias="main", key="ENTER")["ok"]
    assert poll_until(
        lambda: (p := tmux.get_pane(pane_id)) is None or p.pane_dead,
        timeout_s=5.0,
    )
    st = svc.status(lease_id=lid, alias="main")
    assert st["ok"] is False
    assert st["error"]["code"] == "SESSION_DEAD"

    reset = svc.reset(lease_id=lid, alias="main")
    assert reset["ok"] is True
    assert reset["pane_id"] != pane_id
    assert svc.status(lease_id=lid, alias="main")["ok"]


def test_mcp_restart_recovers_registry(terminal_env) -> None:
    svc, mgr, tmp = terminal_env
    lid = _acquire(mgr)
    created = svc.create(lease_id=lid, alias="main")
    assert created["ok"]
    pane_id = created["pane_id"]

    # Simulate MCP process restart: new service instance, same socket+store
    svc2 = TerminalService(TerminalStore(tmp / "terminals.json"))
    listed = svc2.list(lease_id=lid)
    assert listed["ok"] and listed["count"] == 1
    assert listed["terminals"][0]["pane_id"] == pane_id
    assert svc2.status(lease_id=lid, pane_id=pane_id)["ok"]


def test_foreign_lease_cannot_write_without_reclaim(terminal_env) -> None:
    svc, mgr, _ = terminal_env
    lid1 = _acquire(mgr)
    assert svc.create(lease_id=lid1, alias="main")["ok"]
    # expire/release first lease without killing tmux
    assert mgr.release(lease_id=lid1)["ok"]
    lid2 = _acquire(mgr)
    denied = svc.write(lease_id=lid2, alias="main", text="echo no")
    assert denied["ok"] is False
    assert denied["error"]["code"] == "AUTHORIZATION_DENIED"
    # reclaim via reset
    assert svc.reset(lease_id=lid2, alias="main")["ok"]
    assert svc.write(lease_id=lid2, alias="main", text="echo yes")["ok"]


def test_cwd_outside_root_blocked(terminal_env) -> None:
    svc, mgr, _ = terminal_env
    lid = _acquire(mgr)
    bad = svc.create(lease_id=lid, alias="main", cwd="/etc")
    assert bad["ok"] is False
    assert bad["error"]["code"] == "PATH_OUTSIDE_ROOT"


def test_lease_required(terminal_env) -> None:
    svc, _, _ = terminal_env
    out = svc.create(lease_id="", alias="main")
    assert out["error"]["code"] == "LEASE_REQUIRED"


def test_create_reaps_dead_alias(terminal_env) -> None:
    svc, mgr, _ = terminal_env
    lid = _acquire(mgr)
    first = svc.create(lease_id=lid, alias="main")
    assert first["ok"]
    old_id = first["pane_id"]
    assert svc.write(lease_id=lid, alias="main", text="exit")["ok"]
    assert svc.key(lease_id=lid, alias="main", key="ENTER")["ok"]
    assert poll_until(
        lambda: (p := tmux.get_pane(old_id)) is None or p.pane_dead,
        timeout_s=5.0,
    )
    second = svc.create(lease_id=lid, alias="main")
    assert second["ok"] is True
    assert second["pane_id"] != old_id
    # old pane should be gone from registry
    listed = svc.list(lease_id=lid)
    assert listed["count"] == 1
    assert listed["terminals"][0]["pane_id"] == second["pane_id"]
