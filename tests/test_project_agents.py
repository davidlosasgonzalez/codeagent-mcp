"""Subagent definitions are readable, and nothing about them executes."""

from __future__ import annotations

from pathlib import Path

import pytest

from codeagent_mcp.project.service import ProjectIntelligence

AGENT_MD = """---
name: teacher-reviewer
description: Read-only reviewer; contrasts a diff against the project contracts.
tools: Read, Grep, Glob
model: sonnet
---

Never finish without a verdict: PASS / CHANGES REQUESTED / BLOCK.
"""


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectIntelligence:
    from conftest import override_projects

    from codeagent_mcp.workspace import projects as projects_mod

    root = tmp_path / "repo"
    (root / ".claude" / "agents").mkdir(parents=True)
    (root / ".claude" / "agents" / "teacher-reviewer.md").write_text(AGENT_MD, encoding="utf-8")
    (root / ".claude" / "agents" / "notes.txt").write_text("not an agent\n", encoding="utf-8")
    override_projects(
        monkeypatch,
        {"demo": projects_mod.ProjectConfig(name="demo", root=str(root))},
    )
    return ProjectIntelligence(project="demo")


def test_agents_list_indexes_markdown_only(project: ProjectIntelligence) -> None:
    out = project.agents_list()
    assert out["ok"] is True
    assert out["count"] == 1
    agent = out["agents"][0]
    assert agent["agent_id"] == ".claude/agents/teacher-reviewer.md"
    assert agent["name"] == "teacher-reviewer"
    assert agent["tools"] == "Read, Grep, Glob"
    assert "never_spawned" in " ".join(out["warnings"])


def test_agent_read_returns_the_contract(project: ProjectIntelligence) -> None:
    out = project.agent_read(".claude/agents/teacher-reviewer.md")
    assert out["ok"] is True
    assert "PASS / CHANGES REQUESTED / BLOCK" in out["content"]
    assert out["truncated"] is False


def test_agent_read_truncates_on_max_bytes(project: ProjectIntelligence) -> None:
    out = project.agent_read(".claude/agents/teacher-reviewer.md", max_bytes=10)
    assert out["ok"] is True
    assert out["truncated"] is True


@pytest.mark.parametrize(
    "agent_id",
    [
        "../../etc/passwd",
        "/etc/passwd",
        ".claude/agents/../../secret.md",
        "docs/agents/rogue.md",
        ".claude/agents/nested/deep.md",
        ".claude/agents/notes.txt",
    ],
)
def test_agent_read_refuses_paths_outside_the_allowlisted_roots(
    project: ProjectIntelligence, agent_id: str
) -> None:
    out = project.agent_read(agent_id)
    assert out["ok"] is False
    assert out["error"]["code"] in {"NOT_FOUND", "PATH_OUTSIDE_ROOT", "INVALID_ARGUMENT"}


def test_bootstrap_surfaces_agents(project: ProjectIntelligence) -> None:
    out = project.bootstrap()
    assert out["ok"] is True
    assert [a["name"] for a in out["agents"]] == ["teacher-reviewer"]


def test_missing_agents_dir_is_empty_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import override_projects

    from codeagent_mcp.workspace import projects as projects_mod

    root = tmp_path / "bare"
    root.mkdir()
    override_projects(
        monkeypatch,
        {"demo": projects_mod.ProjectConfig(name="demo", root=str(root))},
    )
    out = ProjectIntelligence(project="demo").agents_list()
    assert out["ok"] is True
    assert out["count"] == 0
