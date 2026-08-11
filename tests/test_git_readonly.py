"""git_status / git_diff read-only acceptance."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from conftest import override_projects

from codeagent_mcp.git.service import git_diff, git_status
from codeagent_mcp.server import create_server
from codeagent_mcp.workspace import projects as projects_mod


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "p9@test")
    _git(root, "config", "user.name", "p9")
    (root / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-m", "init")
    override_projects(
        monkeypatch,
        {"demo": projects_mod.ProjectConfig(name="demo", root=str(root))},
    )
    yield root


def test_clean_status(repo: Path) -> None:
    out = git_status(project="demo")
    assert out["ok"] is True
    assert out["branch"] == "main"
    assert out["clean"] is True
    assert out["counts"] == {"staged": 0, "unstaged": 0, "untracked": 0}


def test_modified_untracked(repo: Path) -> None:
    (repo / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    out = git_status(project="demo")
    assert out["ok"] is True
    assert out["clean"] is False
    assert any(e["path"] == "a.txt" for e in out["unstaged"])
    assert any(e["path"] == "new.txt" for e in out["untracked"])


def test_staged(repo: Path) -> None:
    (repo / "a.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    out = git_status(project="demo")
    assert any(e["path"] == "a.txt" for e in out["staged"])


def test_diff_summary_and_body(repo: Path) -> None:
    (repo / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    out = git_diff(project="demo", mode="unstaged")
    assert out["ok"] is True
    assert out["summary"]["files"] >= 1
    assert out["summary"]["insertions"] >= 1
    assert "diff --git" in out["diff"]
    assert out["truncated"] is False


def test_diff_truncated(repo: Path) -> None:
    big = "x" * 5000 + "\n"
    (repo / "a.txt").write_text(big, encoding="utf-8")
    out = git_diff(project="demo", mode="unstaged", max_bytes=2048)
    assert out["ok"] is True
    assert out["truncated"] is True
    assert out["bytes"] <= 2048


def test_path_escape(repo: Path) -> None:
    out = git_status(project="demo", path="../outside")
    assert out["ok"] is False
    assert out["error"]["code"] == "PATH_OUTSIDE_ROOT"
    out2 = git_diff(project="demo", path="../outside")
    assert out2["ok"] is False
    assert out2["error"]["code"] == "PATH_OUTSIDE_ROOT"


def test_not_a_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "norepo"
    root.mkdir()
    override_projects(
        monkeypatch,
        {"demo": projects_mod.ProjectConfig(name="demo", root=str(root))},
    )
    out = git_status(project="demo")
    assert out["ok"] is False
    assert out["error"]["code"] == "NOT_A_GIT_REPO"


def test_tools_registered_readonly() -> None:
    import asyncio

    server = create_server(transport="stdio")

    async def _check() -> None:
        tools = {t.name: t for t in await server.list_tools()}
        assert "git_status" in tools and "git_diff" in tools
        for name in ("git_status", "git_diff"):
            ann = tools[name].annotations
            assert ann is not None
            assert ann.readOnlyHint is True
            assert ann.destructiveHint is False

    asyncio.run(_check())
