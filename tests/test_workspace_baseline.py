"""Acquire-time baselines: my changes, not the dirt that was already there."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codeagent_mcp.git.baseline import BaselineError, diff_since, snapshot
from codeagent_mcp.tools.workspace import set_lease_manager
from codeagent_mcp.workspace.lease_store import LeaseStore
from codeagent_mcp.workspace.leases import LeaseManager


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(root),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


@pytest.fixture()
def dirty_repo(tmp_path: Path) -> Path:
    """A checkout that already has staged, unstaged and untracked work in it."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "committed.txt").write_text("base\n", encoding="utf-8")
    (root / "preexisting.txt").write_text("theirs\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")

    # Dirt that was there before any lease existed.
    (root / "preexisting.txt").write_text("theirs, edited\n", encoding="utf-8")
    (root / "staged_by_them.txt").write_text("theirs, staged\n", encoding="utf-8")
    _git(root, "add", "staged_by_them.txt")
    (root / "untracked_by_them.txt").write_text("theirs, untracked\n", encoding="utf-8")
    return root


@pytest.fixture()
def manager(tmp_path: Path, dirty_repo: Path, monkeypatch: pytest.MonkeyPatch):
    from conftest import override_projects

    from codeagent_mcp.workspace import projects as projects_mod

    monkeypatch.setenv("CODEAGENT_LEASE_STORE", str(tmp_path / "leases.json"))
    override_projects(
        monkeypatch,
        {"demo": projects_mod.ProjectConfig(name="demo", root=str(dirty_repo))},
    )
    mgr = LeaseManager(LeaseStore(tmp_path / "leases.json"), ttl_s=2700)
    set_lease_manager(mgr)
    yield mgr
    set_lease_manager(None)


def test_snapshot_returns_a_tree_and_head(dirty_repo: Path) -> None:
    base = snapshot(str(dirty_repo))
    assert len(base["tree"]) == 40
    assert len(base["head"]) == 40
    assert base["taken_at"].endswith("Z")


def test_snapshot_leaves_index_and_worktree_alone(dirty_repo: Path) -> None:
    index = dirty_repo / ".git" / "index"
    before = index.read_bytes()
    head_before = (dirty_repo / ".git" / "HEAD").read_text(encoding="utf-8")

    snapshot(str(dirty_repo))

    assert index.read_bytes() == before
    assert (dirty_repo / ".git" / "HEAD").read_text(encoding="utf-8") == head_before
    assert (dirty_repo / "untracked_by_them.txt").exists()


def test_snapshot_rejects_a_non_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(BaselineError):
        snapshot(str(plain))


def test_acquire_records_a_baseline(manager: LeaseManager, dirty_repo: Path) -> None:
    acq = manager.acquire(project="demo")
    assert acq["ok"] is True
    assert acq["baseline"]["tree"]
    assert acq["tmpdir"]


def test_diff_since_acquire_excludes_preexisting_dirt(
    manager: LeaseManager, dirty_repo: Path
) -> None:
    acq = manager.acquire(project="demo")
    baseline = acq["baseline"]

    (dirty_repo / "mine.txt").write_text("mine\n", encoding="utf-8")
    (dirty_repo / "committed.txt").write_text("base\nmine\n", encoding="utf-8")

    out = diff_since(project="demo", root=str(dirty_repo), baseline=baseline)
    assert out["ok"] is True
    changed = {row["path"] for row in out["files"]}
    assert changed == {"mine.txt", "committed.txt"}
    assert "preexisting.txt" not in changed
    assert "staged_by_them.txt" not in changed
    assert "untracked_by_them.txt" not in changed
    assert out["summary"]["files"] == 2


def test_diff_since_acquire_survives_staging_and_commits(
    manager: LeaseManager, dirty_repo: Path
) -> None:
    """Staging state must not change the answer — that is the whole point."""
    acq = manager.acquire(project="demo")
    baseline = acq["baseline"]

    (dirty_repo / "mine.txt").write_text("mine\n", encoding="utf-8")
    _git(dirty_repo, "add", "mine.txt")
    _git(dirty_repo, "commit", "-q", "-m", "mine")

    out = diff_since(project="demo", root=str(dirty_repo), baseline=baseline)
    assert out["ok"] is True
    assert {row["path"] for row in out["files"]} == {"mine.txt"}
    assert out["head_moved"] is True


def test_clean_worktree_reports_no_changes(manager: LeaseManager, dirty_repo: Path) -> None:
    acq = manager.acquire(project="demo")
    out = diff_since(project="demo", root=str(dirty_repo), baseline=acq["baseline"])
    assert out["ok"] is True
    assert out["summary"]["files"] == 0
    assert out["diff"] == ""


def test_missing_baseline_is_an_explicit_error(dirty_repo: Path) -> None:
    out = diff_since(project="demo", root=str(dirty_repo), baseline=None)
    assert out["ok"] is False
    assert out["error"]["code"] == "BASELINE_UNAVAILABLE"


def test_baseline_can_be_disabled_by_the_operator(
    manager: LeaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEAGENT_LEASE_BASELINE", "0")
    acq = manager.acquire(project="demo")
    assert acq["ok"] is True
    assert "baseline" not in acq
