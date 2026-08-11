"""exec_run acceptance tests."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from codeagent_mcp.exec.gate import ExecLeaseGate, set_exec_gate
from codeagent_mcp.tools.exec_run import run_exec_run
from codeagent_mcp.tools.workspace import set_lease_manager
from codeagent_mcp.workspace import projects as projects_mod
from codeagent_mcp.workspace.lease_store import LeaseStore
from codeagent_mcp.workspace.leases import LeaseManager


@pytest.fixture()
def exec_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "marker.txt").write_text("ok\n", encoding="utf-8")
    store_path = tmp_path / "leases.json"
    monkeypatch.setenv("CODEAGENT_LEASE_STORE", str(store_path))
    monkeypatch.setenv("CODEAGENT_LEASE_TTL_S", "2700")

    # Point the registered project at a tmp root; nothing outside it is touched.
    from conftest import override_projects

    override_projects(
        monkeypatch,
        {"demo": projects_mod.ProjectConfig(name="demo", root=str(root))},
    )

    manager = LeaseManager(LeaseStore(store_path), ttl_s=2700)
    set_lease_manager(manager)
    set_exec_gate(ExecLeaseGate())
    acq = manager.acquire(project="demo")
    assert acq["ok"] is True
    yield {
        "mgr": manager,
        "root": root,
        "lease_id": acq["lease_id"],
    }
    set_lease_manager(None)
    set_exec_gate(None)


def test_exit_zero(exec_env) -> None:
    out = run_exec_run(
        lease_id=exec_env["lease_id"],
        command=["/bin/echo", "hello-c202"],
        cwd=str(exec_env["root"]),
    )
    assert out["ok"] is True
    assert out["exit_code"] == 0
    assert out["timed_out"] is False
    assert "hello-c202" in out["stdout"]
    assert out["stdout_truncated"] is False


def test_exit_nonzero(exec_env) -> None:
    out = run_exec_run(
        lease_id=exec_env["lease_id"],
        command=["/bin/sh", "-c", "exit 7"],
        cwd=str(exec_env["root"]),
    )
    assert out["ok"] is True
    assert out["exit_code"] == 7


def test_timeout_kills_process_group(exec_env) -> None:
    # Parent spawns a child sleep; killpg must reap the tree.
    script = "import os, time, subprocess\nsubprocess.Popen(['sleep', '30'])\ntime.sleep(30)\n"
    out = run_exec_run(
        lease_id=exec_env["lease_id"],
        command=["python3", "-c", script],
        cwd=str(exec_env["root"]),
        timeout_s=1,
    )
    assert out["ok"] is True
    assert out["timed_out"] is True
    assert out["signal"] == "SIGKILL"
    assert out["duration_ms"] < 15_000


def test_stdout_truncated(exec_env) -> None:
    out = run_exec_run(
        lease_id=exec_env["lease_id"],
        command=["python3", "-c", "print('A'*5000)"],
        cwd=str(exec_env["root"]),
        max_output_bytes=100,
    )
    assert out["ok"] is True
    assert out["stdout_truncated"] is True
    assert len(out["stdout"].encode()) <= 100 + 16  # utf-8 safety slack


def test_stderr_large(exec_env) -> None:
    out = run_exec_run(
        lease_id=exec_env["lease_id"],
        command=["python3", "-c", "import sys; sys.stderr.write('E'*2000); sys.stderr.flush()"],
        cwd=str(exec_env["root"]),
        max_output_bytes=200,
    )
    assert out["ok"] is True
    assert out["stderr_truncated"] is True
    assert "E" in out["stderr"]


def test_command_not_found(exec_env) -> None:
    out = run_exec_run(
        lease_id=exec_env["lease_id"],
        command=["/definitely/not/a/binary-c202"],
        cwd=str(exec_env["root"]),
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "NOT_FOUND"


def test_lease_required(exec_env) -> None:
    out = run_exec_run(lease_id="", command=["/bin/echo", "x"], cwd=str(exec_env["root"]))
    assert out["ok"] is False
    assert out["error"]["code"] == "LEASE_REQUIRED"


def test_lease_expired(exec_env) -> None:
    out = run_exec_run(
        lease_id="bogus-lease",
        command=["/bin/echo", "x"],
        cwd=str(exec_env["root"]),
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "LEASE_EXPIRED"


def test_cwd_outside_root(exec_env) -> None:
    out = run_exec_run(
        lease_id=exec_env["lease_id"],
        command=["/bin/echo", "x"],
        cwd="/tmp",
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "PATH_OUTSIDE_ROOT"


def test_env_override_blocked(exec_env) -> None:
    out = run_exec_run(
        lease_id=exec_env["lease_id"],
        command=["/bin/echo", "x"],
        cwd=str(exec_env["root"]),
        env_overrides={"PATH": "/evil"},
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "RISK_BLOCKED"


def test_env_override_allowlisted(exec_env) -> None:
    out = run_exec_run(
        lease_id=exec_env["lease_id"],
        command=["/usr/bin/env"],
        cwd=str(exec_env["root"]),
        env_overrides={"TEST_DATA_ROOT": "/tmp/codeagent-test-data"},
    )
    assert out["ok"] is True
    assert "TEST_DATA_ROOT=/tmp/codeagent-test-data" in out["stdout"]


def test_concurrent_exec_same_lease(exec_env) -> None:
    results: list[dict] = []

    def worker(delay_cmd: list[str]) -> None:
        results.append(
            run_exec_run(
                lease_id=exec_env["lease_id"],
                command=delay_cmd,
                cwd=str(exec_env["root"]),
                timeout_s=5,
            )
        )

    t1 = threading.Thread(target=worker, args=(["/bin/sleep", "1"],))
    t1.start()
    time.sleep(0.1)
    t2 = threading.Thread(target=worker, args=(["/bin/echo", "second"],))
    t2.start()
    t1.join()
    t2.join()
    codes = {r.get("error", {}).get("code") for r in results if not r.get("ok")}
    oks = [r for r in results if r.get("ok")]
    assert "PROCESS_RUNNING" in codes
    assert len(oks) == 1


def test_project_env_is_injected(exec_env, monkeypatch) -> None:
    """A project's server-side env map reaches the child process."""
    from conftest import override_projects

    override_projects(
        monkeypatch,
        {
            "demo": projects_mod.ProjectConfig(
                name="demo",
                root=str(exec_env["root"]),
                env={"MYAPP_DATA_ROOT": "/srv/data"},
            )
        },
    )
    out = run_exec_run(
        lease_id=exec_env["lease_id"],
        command=["/usr/bin/env"],
        cwd=str(exec_env["root"]),
    )
    assert out["ok"] is True
    assert "MYAPP_DATA_ROOT=/srv/data" in out["stdout"]


def test_child_umask_is_not_inherited(exec_env) -> None:
    """A hardened service umask must not follow commands into the project tree.

    Inheriting one silently creates files that only the server account can read,
    which locks out the service accounts that run the code.
    """
    previous = os.umask(0o077)
    try:
        out = run_exec_run(
            lease_id=exec_env["lease_id"],
            command=["/bin/sh", "-c", "umask"],
            cwd=str(exec_env["root"]),
        )
    finally:
        os.umask(previous)
    assert out["ok"] is True
    assert out["stdout"].strip() == "0022"


def test_child_umask_is_configurable(exec_env, monkeypatch) -> None:
    monkeypatch.setenv("CODEAGENT_EXEC_UMASK", "027")
    out = run_exec_run(
        lease_id=exec_env["lease_id"],
        command=["/bin/sh", "-c", "umask"],
        cwd=str(exec_env["root"]),
    )
    assert out["ok"] is True
    assert out["stdout"].strip() == "0027"


def test_project_env_cannot_override_reserved_keys(tmp_path) -> None:
    """Even server-side config may not touch loader or process variables."""
    from codeagent_mcp.exec.env import apply_project_env

    # A merged env always carries the pinned temp root; mirror that here.
    base = {"PATH": "/usr/bin", "TMPDIR": str(tmp_path)}
    merged = apply_project_env(base, {"PATH": "/evil", "LD_PRELOAD": "/evil.so", "OK_VAR": "kept"})
    assert merged["PATH"] == "/usr/bin"
    assert "LD_PRELOAD" not in merged
    assert merged["OK_VAR"] == "kept"


def test_server_registers_exec_run(exec_env) -> None:
    import asyncio

    from codeagent_mcp.server import create_server

    server = create_server(transport="stdio")

    async def _names() -> list[str]:
        tools = await server.list_tools()
        return sorted(t.name for t in tools)

    names = asyncio.run(_names())
    assert "exec_run" in names
