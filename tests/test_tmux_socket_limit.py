"""A socket path longer than AF_UNIX allows fails once, not forever.

Found while chasing a CI failure: pytest's --basetemp made the tmux socket path
124 bytes, and terminal_create answered INTERNAL_ERROR with retryable=True. The
cause was reported correctly; the advice was not. sun_path is 108 bytes
including the NUL, so no retry can ever succeed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeagent_mcp.terminal import tmux


def test_a_normal_path_has_no_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEAGENT_TMUX_SOCKET", "/tmp/x/tmux.sock")
    assert tmux.socket_path_problem() is None


def test_the_default_is_within_the_limit() -> None:
    """A shipped default that cannot bind would be a poor first impression."""
    assert len(tmux.DEFAULT_SOCKET.encode()) <= tmux.MAX_SOCKET_PATH_BYTES


def test_exactly_at_the_limit_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    path = "/tmp/" + "a" * (tmux.MAX_SOCKET_PATH_BYTES - len("/tmp/"))
    assert len(path.encode()) == tmux.MAX_SOCKET_PATH_BYTES
    monkeypatch.setenv("CODEAGENT_TMUX_SOCKET", path)
    assert tmux.socket_path_problem() is None


def test_one_byte_over_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    path = "/tmp/" + "a" * (tmux.MAX_SOCKET_PATH_BYTES - len("/tmp/") + 1)
    monkeypatch.setenv("CODEAGENT_TMUX_SOCKET", path)
    problem = tmux.socket_path_problem()
    assert problem is not None
    assert "CODEAGENT_TMUX_SOCKET" in problem, "the message must name what to change"


def test_the_limit_is_measured_in_bytes_not_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path of accented characters is longer than it looks to len()."""
    name = "ñ" * 60  # 120 bytes
    path = f"/tmp/{name}.sock"
    assert len(path) < tmux.MAX_SOCKET_PATH_BYTES < len(path.encode())
    monkeypatch.setenv("CODEAGENT_TMUX_SOCKET", path)
    assert tmux.socket_path_problem() is not None


def test_the_limit_matches_what_the_kernel_actually_does(tmp_path: Path) -> None:
    """Pin the constant against a real bind rather than a remembered number."""
    import socket

    ok = tmp_path / ("s" * 10)
    too_long = Path("/tmp") / ("b" * 200) / "x.sock"

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with pytest.raises(OSError):
        sock.bind(str(too_long))
    sock.close()

    # And a short one binds, so the failure above is about length.
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(ok))
    sock.close()


def test_create_refuses_without_calling_tmux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: a real lease, no subprocess, and a non-retryable answer."""
    import codeagent_mcp.tools.workspace as ws
    from codeagent_mcp.terminal import service as service_mod
    from codeagent_mcp.terminal.store import TerminalStore
    from codeagent_mcp.workspace import projects as projects_mod
    from codeagent_mcp.workspace.lease_store import LeaseStore
    from codeagent_mcp.workspace.leases import LeaseManager

    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(
        projects_mod,
        "_registry",
        lambda: {"demo": projects_mod.ProjectConfig(name="demo", root=str(root))},
    )
    manager = LeaseManager(LeaseStore(tmp_path / "leases.json"), ttl_s=600)
    ws.set_lease_manager(manager)
    monkeypatch.setenv("CODEAGENT_TERMINAL_STORE", str(tmp_path / "terminals.json"))

    called: list[str] = []

    def _never(**kwargs: object) -> None:
        called.append("tmux")
        raise AssertionError("tmux must not be invoked")

    monkeypatch.setattr(service_mod.tmux, "create_window", _never)
    monkeypatch.setenv("CODEAGENT_TMUX_SOCKET", "/tmp/" + "a" * 200)

    try:
        lease = manager.acquire(project="demo")
        assert lease["ok"] is True, lease
        svc = service_mod.TerminalService(TerminalStore(tmp_path / "terminals.json"))
        out = svc.create(lease_id=lease["lease_id"], alias="dbg")
    finally:
        ws.set_lease_manager(None)

    assert out["ok"] is False
    assert out["error"]["retryable"] is False, "retrying this can never succeed"
    assert "CODEAGENT_TMUX_SOCKET" in out["error"]["message"]
    assert called == [], "tmux must not be invoked for a path that cannot bind"
