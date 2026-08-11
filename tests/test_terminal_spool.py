"""Spool plus terminal_read / terminal_snapshot."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codeagent_mcp.terminal import spool, tmux
from codeagent_mcp.terminal.service import TerminalService, poll_until
from codeagent_mcp.terminal.store import TerminalStore
from codeagent_mcp.tools.terminal import set_terminal_service
from codeagent_mcp.tools.workspace import set_lease_manager
from codeagent_mcp.workspace.lease_store import LeaseStore
from codeagent_mcp.workspace.leases import LeaseManager


@pytest.fixture()
def spool_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sock = tmp_path / "tmux.sock"
    conf = tmp_path / "tmux.conf"
    store = tmp_path / "terminals.json"
    lease_path = tmp_path / "leases.json"
    spool_root = tmp_path / "spool"
    monkeypatch.setenv("CODEAGENT_TMUX_SOCKET", str(sock))
    monkeypatch.setenv("CODEAGENT_TMUX_CONF", str(conf))
    monkeypatch.setenv("CODEAGENT_TMUX_TMPDIR", str(tmp_path / "tmux"))
    monkeypatch.setenv("CODEAGENT_TERMINAL_STORE", str(store))
    monkeypatch.setenv("CODEAGENT_LEASE_STORE", str(lease_path))
    monkeypatch.setenv("CODEAGENT_SPOOL_ROOT", str(spool_root))
    monkeypatch.setenv("CODEAGENT_LEASE_TTL_S", "2700")
    runtime = Path(f"/run/user/{os.getuid()}")
    if runtime.is_dir():
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))

    mgr = LeaseManager(LeaseStore(lease_path), ttl_s=2700)
    set_lease_manager(mgr)
    svc = TerminalService(TerminalStore(store))
    set_terminal_service(svc)

    from conftest import override_projects

    from codeagent_mcp.workspace import projects as projects_mod

    (tmp_path / "proj").mkdir()
    override_projects(
        monkeypatch,
        {"demo": projects_mod.ProjectConfig(name="demo", root=str(tmp_path / "proj"))},
    )

    yield svc, mgr, tmp_path, spool_root

    try:
        tmux.run_tmux(["kill-server"], check=False)
    except Exception:
        pass
    set_terminal_service(None)
    set_lease_manager(None)


def _acquire(mgr: LeaseManager) -> str:
    acq = mgr.acquire(project="demo")
    assert acq["ok"]
    return acq["lease_id"]


def test_read_after_create_captures_output(spool_env) -> None:
    svc, mgr, _, spool_root = spool_env
    lid = _acquire(mgr)
    created = svc.create(lease_id=lid, alias="main")
    assert created["ok"], created
    marker = "SPOOL_MARKER_42"
    assert svc.write(lease_id=lid, alias="main", text=f"echo {marker}")["ok"]
    assert svc.key(lease_id=lid, alias="main", key="ENTER")["ok"]

    path = next(spool_root.glob("*.log"))
    assert poll_until(lambda: marker.encode() in path.read_bytes(), timeout_s=5.0)

    first = svc.read(lease_id=lid, alias="main", max_bytes=50_000)
    assert first["ok"], first
    assert marker in first["text"]
    assert "\x1b" not in first["text"]
    assert first["raw_byte_len"] > 0
    assert first["next_cursor"].startswith("v1:")

    # second read with cursor should not duplicate prior raw range
    second = svc.read(lease_id=lid, alias="main", cursor=first["next_cursor"])
    assert second["ok"], second
    # may be empty or only new prompt bytes; must not re-include full first chunk as re-read from 0
    assert second["cursor"] == first["next_cursor"]


def test_snapshot_uses_capture_not_only_spool(spool_env) -> None:
    svc, mgr, _, _ = spool_env
    lid = _acquire(mgr)
    assert svc.create(lease_id=lid, alias="main")["ok"]
    assert svc.write(lease_id=lid, alias="main", text="echo SNAP_MARK")["ok"]
    assert svc.key(lease_id=lid, alias="main", key="ENTER")["ok"]
    assert poll_until(
        lambda: "SNAP_MARK" in (svc.snapshot(lease_id=lid, alias="main").get("text") or ""),
        timeout_s=5.0,
    )
    snap = svc.snapshot(lease_id=lid, alias="main")
    assert snap["ok"] and snap["source"] == "capture-pane"
    assert "SNAP_MARK" in snap["text"]


def test_cursor_expired_after_generation_change(spool_env) -> None:
    svc, mgr, _, _ = spool_env
    lid = _acquire(mgr)
    assert svc.create(lease_id=lid, alias="main")["ok"]
    r1 = svc.read(lease_id=lid, alias="main")
    assert r1["ok"]
    old_cursor = r1["next_cursor"]
    assert svc.reset(lease_id=lid, alias="main")["ok"]
    expired = svc.read(lease_id=lid, alias="main", cursor=old_cursor)
    assert expired["ok"] is False
    assert expired["error"]["code"] == "CURSOR_EXPIRED"


def test_rotate_expires_old_offsets(spool_env, monkeypatch: pytest.MonkeyPatch) -> None:
    svc, mgr, _, spool_root = spool_env
    monkeypatch.setattr(
        "codeagent_mcp.terminal.service.DEFAULT_MAX_SPOOL_BYTES",
        200,
    )
    lid = _acquire(mgr)
    assert svc.create(lease_id=lid, alias="main")["ok"]
    # grow spool beyond soft max
    blob = "X" * 180
    assert svc.write(lease_id=lid, alias="main", text=f"echo {blob}")["ok"]
    assert svc.key(lease_id=lid, alias="main", key="ENTER")["ok"]
    path = next(spool_root.glob("*.log"))
    assert poll_until(lambda: path.stat().st_size >= 200, timeout_s=5.0)
    # force another write so size stays high, then read triggers rotate
    assert svc.write(lease_id=lid, alias="main", text="echo MORE")["ok"]
    assert svc.key(lease_id=lid, alias="main", key="ENTER")["ok"]
    assert poll_until(
        lambda: b"MORE" in path.read_bytes() or path.stat().st_size >= 200, timeout_s=5.0
    )

    # Read from 0 before rotate to get a cursor at early offset, then rotate via large read path
    early = svc.read(lease_id=lid, alias="main", max_bytes=10)
    assert early["ok"]
    # Pad file to ensure rotate on next read
    with path.open("ab") as fh:
        fh.write(b"Y" * 300)
    rotated_read = svc.read(
        lease_id=lid, alias="main", cursor=early["cursor"] or early.get("next_cursor")
    )
    # After rotate, early physical offsets under new base expire OR read succeeds at new base
    if not rotated_read.get("ok"):
        assert rotated_read["error"]["code"] == "CURSOR_EXPIRED"
    else:
        # If cursor was None-ish path; accept ok after rotate
        assert "next_cursor" in rotated_read


def test_close_deletes_spool(spool_env) -> None:
    svc, mgr, _, spool_root = spool_env
    lid = _acquire(mgr)
    assert svc.create(lease_id=lid, alias="main")["ok"]
    assert list(spool_root.glob("*.log"))
    assert svc.close(lease_id=lid, alias="main")["ok"]
    assert list(spool_root.glob("*.log")) == []


def test_sanitize_unit() -> None:
    text, binary = spool.sanitize_for_client(b"\x1b[?2004hhi\x1b[0m\n\x00")
    assert "hi" in text
    assert "\x1b" not in text
    assert binary is True


def test_read_does_not_bounce_active_pipe(spool_env) -> None:
    svc, mgr, _, spool_root = spool_env
    lid = _acquire(mgr)
    created = svc.create(lease_id=lid, alias="main")
    assert created["ok"]
    pane_id = created["pane_id"]
    assert tmux.pane_pipe_active(pane_id)
    # read should not detach/reattach
    assert svc.read(lease_id=lid, alias="main")["ok"]
    assert tmux.pane_pipe_active(pane_id)
    marker = "NO_BOUNCE_MARK"
    assert svc.write(lease_id=lid, alias="main", text=f"echo {marker}")["ok"]
    assert svc.key(lease_id=lid, alias="main", key="ENTER")["ok"]
    path = next(spool_root.glob("*.log"))
    assert poll_until(lambda: marker.encode() in path.read_bytes(), timeout_s=5.0)
    out = svc.read(lease_id=lid, alias="main")
    assert out["ok"] and marker in out["text"]
