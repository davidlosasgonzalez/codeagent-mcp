"""Project instruction discovery tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from codeagent_mcp.project.service import ProjectIntelligence
from codeagent_mcp.server import create_server
from codeagent_mcp.workspace import projects as projects_mod


@pytest.fixture()
def mono(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "AGENTS.md").write_text("# agents root\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("# claude root\nSee @MISSING.md\n", encoding="utf-8")
    (root / "frontend").mkdir()
    (root / "frontend" / "CLAUDE.md").write_text("# claude frontend only\n", encoding="utf-8")
    (root / "backend").mkdir()
    (root / "backend" / "app.py").write_text("print('x')\n", encoding="utf-8")
    rules = root / ".cursor" / "rules"
    rules.mkdir(parents=True)
    (rules / "always.mdc").write_text(
        "---\ndescription: always rule\nalwaysApply: true\n---\nALWAYS\n",
        encoding="utf-8",
    )
    (rules / "front.mdc").write_text(
        "---\ndescription: frontend globs\nglobs: frontend/**\nalwaysApply: false\n---\nFRONT\n",
        encoding="utf-8",
    )
    (rules / "back.mdc").write_text(
        "---\ndescription: backend globs\nglobs: backend/**\nalwaysApply: false\n---\nBACK\n",
        encoding="utf-8",
    )
    (rules / "manual.mdc").write_text(
        "---\ndescription: agent requested rule\nalwaysApply: false\n---\nMANUAL\n",
        encoding="utf-8",
    )
    # hostile symlink escape
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("nope\n", encoding="utf-8")
    (root / "escape").symlink_to(outside)

    from conftest import override_projects

    override_projects(
        monkeypatch,
        {"demo": projects_mod.ProjectConfig(name="demo", root=str(root))},
    )
    return root


def test_bootstrap_manifest_no_bodies(mono: Path) -> None:
    out = ProjectIntelligence(project="demo").bootstrap()
    assert out["ok"]
    assert "skills_discovery_deferred" not in out["warnings"]
    assert all("content" not in s for s in out["instruction_sources"])
    assert all("content" not in s for s in out["skills"])
    types = {s["source_type"] for s in out["instruction_sources"]}
    assert "agents_md" in types and "claude_md" in types and "cursor_rule" in types


def test_broken_at_ref_warning(mono: Path) -> None:
    out = ProjectIntelligence(project="demo").bootstrap()
    claude = next(s for s in out["instruction_sources"] if s["relative"] == "CLAUDE.md")
    assert any("broken_reference" in w for w in claude["warnings"])


def test_paths_frontend_excludes_backend_rule(mono: Path) -> None:
    out = ProjectIntelligence(project="demo").instructions(paths=["frontend/page.tsx"])
    assert out["ok"]
    rels = {i["relative"] for i in out["instructions"]}
    assert "frontend/CLAUDE.md" in rels
    assert ".cursor/rules/front.mdc" in rels
    assert ".cursor/rules/back.mdc" not in rels
    bodies = {i["relative"]: i["content"] for i in out["instructions"]}
    assert "FRONT" in bodies[".cursor/rules/front.mdc"]


def test_empty_paths_always_only(mono: Path) -> None:
    out = ProjectIntelligence(project="demo").instructions(paths=[])
    assert out["ok"]
    acts = {i["activation"] for i in out["instructions"]}
    assert acts <= {"always"}
    rels = {i["relative"] for i in out["instructions"]}
    assert ".cursor/rules/manual.mdc" not in rels
    assert "frontend/CLAUDE.md" not in rels


def test_manual_not_always(mono: Path) -> None:
    boot = ProjectIntelligence(project="demo").bootstrap()
    manual = next(s for s in boot["instruction_sources"] if s["relative"].endswith("manual.mdc"))
    assert manual["activation"] == "agent_requested"
    out = ProjectIntelligence(project="demo").instructions(paths=[], include_agent_requested=True)
    rels = {i["relative"] for i in out["instructions"]}
    assert ".cursor/rules/manual.mdc" in rels


def test_no_silent_merge_flag(mono: Path) -> None:
    out = ProjectIntelligence(project="demo").instructions(paths=["backend/app.py"])
    assert out["merge_policy"] == "none_explicit_provenance_only"
    assert out["count"] >= 1


def test_server_registers(mono: Path) -> None:
    import asyncio

    server = create_server(transport="stdio")

    async def _names() -> set[str]:
        return {t.name for t in await server.list_tools()}

    names = asyncio.run(_names())
    assert "project_bootstrap" in names
    assert "project_instructions" in names
