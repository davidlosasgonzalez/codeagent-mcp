"""Workspace lease tests."""

from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codeagent_mcp.server import create_server
from codeagent_mcp.tools.workspace import set_lease_manager
from codeagent_mcp.workspace.lease_store import LeaseStore
from codeagent_mcp.workspace.leases import LeaseManager


@pytest.fixture()
def lease_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store_path = tmp_path / "leases.json"
    monkeypatch.setenv("CODEAGENT_LEASE_STORE", str(store_path))
    monkeypatch.setenv("CODEAGENT_LEASE_TTL_S", "2700")
    from conftest import override_projects

    from codeagent_mcp.workspace import projects as projects_mod

    root = str(tmp_path / "example-app")
    Path(root).mkdir(parents=True, exist_ok=True)
    override_projects(
        monkeypatch,
        {"demo": projects_mod.ProjectConfig(name="demo", root=root)},
    )
    manager = LeaseManager(LeaseStore(store_path), ttl_s=2700)
    set_lease_manager(manager)
    yield manager, store_path, root
    set_lease_manager(None)


def test_acquire_status_release_roundtrip(lease_env) -> None:
    mgr, _, root = lease_env
    acq = mgr.acquire(project="demo", mode="exclusive")
    assert acq["ok"] is True
    assert acq["status"] == "acquired"
    assert acq["root"] == root
    lid = acq["lease_id"]

    st = mgr.status(project="demo")
    assert st["ok"] is True and st["held"] is True and st["status"] == "held"
    assert "lease_id" not in st  # no foreign token leak

    mine = mgr.status(lease_id=lid)
    assert mine["ok"] is True and mine["held"] is True and mine["lease_id"] == lid

    rel = mgr.release(lease_id=lid)
    assert rel["ok"] is True and rel["status"] == "released"
    rel2 = mgr.release(lease_id=lid)
    assert rel2["ok"] is True and rel2["status"] == "already_released"

    st2 = mgr.status(project="demo")
    assert st2["held"] is False and st2["status"] == "free"


def test_second_acquire_busy(lease_env) -> None:
    mgr, _, _ = lease_env
    first = mgr.acquire(project="demo")
    second = mgr.acquire(project="demo")
    assert first["ok"] is True
    assert second["ok"] is False
    assert second["error"]["code"] == "LEASE_BUSY"
    assert "current_expires_at" in second["error"]
    assert "lease_id" not in second["error"]


def test_same_holder_reclaims_after_lost_lease_id(lease_env) -> None:
    mgr, _, _ = lease_env
    first = mgr.acquire(project="demo", holder_sub="github|42")
    assert first["ok"] is True
    lid = first["lease_id"]
    # Simulate ChatGPT reconnect without lease_id but same OAuth sub.
    again = mgr.acquire(project="demo", holder_sub="github|42")
    assert again["ok"] is True
    assert again["lease_id"] == lid
    assert again["status"] == "reclaimed"
    # Different sub still busy.
    other = mgr.acquire(project="demo", holder_sub="github|99")
    assert other["ok"] is False and other["error"]["code"] == "LEASE_BUSY"


def test_status_reveals_lease_id_only_to_holder(lease_env) -> None:
    mgr, _, _ = lease_env
    acq = mgr.acquire(project="demo", holder_sub="github|42")
    lid = acq["lease_id"]
    foreign = mgr.status(project="demo", holder_sub="github|99")
    assert foreign["held"] is True
    assert "lease_id" not in foreign
    mine = mgr.status(project="demo", holder_sub="github|42")
    assert mine["lease_id"] == lid
    assert mine.get("holder_match") is True


def test_renew_extends_expiry(lease_env) -> None:
    clock = {"t": datetime(2026, 8, 9, 12, 0, tzinfo=UTC)}

    def now():
        return clock["t"]

    mgr, path, _ = lease_env
    mgr._now = now
    acq = mgr.acquire(project="demo")
    lid = acq["lease_id"]
    exp1 = acq["expires_at"]
    clock["t"] = clock["t"] + timedelta(minutes=10)
    renewed = mgr.acquire(project="demo", lease_id=lid)
    assert renewed["ok"] is True and renewed["status"] == "renewed"
    assert renewed["expires_at"] > exp1


def test_expiry_allows_new_acquire(lease_env) -> None:
    clock = {"t": datetime(2026, 8, 9, 12, 0, tzinfo=UTC)}

    def now():
        return clock["t"]

    mgr, _, _ = lease_env
    mgr._now = now
    mgr.ttl_s = 60
    acq = mgr.acquire(project="demo")
    old_id = acq["lease_id"]
    clock["t"] = clock["t"] + timedelta(seconds=120)
    again = mgr.acquire(project="demo")
    assert again["ok"] is True and again["status"] == "acquired"
    assert again["lease_id"] != old_id
    # old id must not reclaim
    bad = mgr.acquire(project="demo", lease_id=old_id)
    assert bad["ok"] is False
    assert bad["error"]["code"] in {"LEASE_BUSY", "LEASE_EXPIRED"}


def test_invalid_lease_status(lease_env) -> None:
    mgr, _, _ = lease_env
    st = mgr.status(lease_id="does-not-exist")
    assert st["ok"] is True and st["status"] == "unknown" and st["held"] is False


def test_restart_reloads_store(lease_env) -> None:
    mgr, path, _ = lease_env
    acq = mgr.acquire(project="demo")
    lid = acq["lease_id"]
    mgr2 = LeaseManager(LeaseStore(path), ttl_s=2700)
    st = mgr2.status(lease_id=lid)
    assert st["held"] is True


def test_corrupt_store_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from conftest import override_projects

    from codeagent_mcp.workspace import projects as projects_mod

    root = tmp_path / "example-app"
    root.mkdir()
    override_projects(
        monkeypatch,
        {"demo": projects_mod.ProjectConfig(name="demo", root=str(root))},
    )
    path = tmp_path / "leases.json"
    path.write_text("{not-json", encoding="utf-8")
    mgr = LeaseManager(LeaseStore(path), ttl_s=60)
    out = mgr.acquire(project="demo")
    assert out["ok"] is False
    assert out["error"]["code"] == "INTERNAL_ERROR"


def test_concurrent_acquires_one_winner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from conftest import override_projects

    from codeagent_mcp.workspace import projects as projects_mod

    root = tmp_path / "example-app"
    root.mkdir()
    override_projects(
        monkeypatch,
        {"demo": projects_mod.ProjectConfig(name="demo", root=str(root))},
    )
    path = tmp_path / "leases.json"
    results: list[dict] = []

    def worker() -> None:
        mgr = LeaseManager(LeaseStore(path), ttl_s=2700)
        results.append(mgr.acquire(project="demo"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wins = [r for r in results if r.get("ok") is True]
    busy = [r for r in results if r.get("ok") is False and r["error"]["code"] == "LEASE_BUSY"]
    assert len(wins) == 1
    assert len(busy) == 7
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["leases"]) == 1


def test_server_registers_workspace_tools(lease_env) -> None:
    server = create_server(transport="stdio")

    async def _names() -> list[str]:
        tools = await server.list_tools()
        return sorted(t.name for t in tools)

    names = asyncio.run(_names())
    assert set(names) >= {
        "server_info",
        "workspace_acquire",
        "workspace_release",
        "workspace_status",
        "exec_run",
        "fs_stat",
        "fs_list",
        "fs_read",
        "fs_search",
        "fs_apply_patch",
        "project_bootstrap",
        "project_instructions",
        "project_skills_list",
        "project_skill_read",
    }


def _mp_worker(store: str, projects_file: str, q) -> None:
    import os

    os.environ["CODEAGENT_PROJECTS_FILE"] = projects_file
    mgr = LeaseManager(LeaseStore(Path(store)), ttl_s=2700)
    q.put(mgr.acquire(project="demo"))


def test_multiprocess_acquires_one_winner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import yaml

    root = tmp_path / "example-app"
    root.mkdir()
    projects_file = tmp_path / "projects.yaml"
    projects_file.write_text(
        yaml.safe_dump({"projects": [{"id": "demo", "root": str(root), "writable": False}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEAGENT_PROJECTS_FILE", str(projects_file))
    store = tmp_path / "leases-mp.json"
    q: mp.Queue = mp.Queue()
    procs = [
        mp.Process(target=_mp_worker, args=(str(store), str(projects_file), q)) for _ in range(4)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0
    results = [q.get(timeout=5) for _ in range(4)]
    wins = [r for r in results if r.get("ok") is True]
    busy = [r for r in results if r.get("ok") is False and r["error"]["code"] == "LEASE_BUSY"]
    assert len(wins) == 1
    assert len(busy) == 3
    assert "retryable" in busy[0]["error"]
    assert "current_expires_at" in busy[0]["error"]
