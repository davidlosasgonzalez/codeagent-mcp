"""Agent Skills discovery tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from codeagent_mcp.project.service import ProjectIntelligence
from codeagent_mcp.server import create_server
from codeagent_mcp.workspace import projects as projects_mod


@pytest.fixture()
def skills_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "repo"
    root.mkdir()
    # portable
    p = root / ".claude" / "skills" / "portable"
    p.mkdir(parents=True)
    (p / "SKILL.md").write_text(
        "---\nname: portable\ndescription: A portable skill\n---\n# Hello\nDo X.\n",
        encoding="utf-8",
    )
    (p / "notes.txt").write_text("support\n", encoding="utf-8")
    # with extensions + bang
    e = root / ".claude" / "skills" / "extended"
    e.mkdir(parents=True)
    (e / "SKILL.md").write_text(
        "---\nname: extended\ndescription: Has extensions\nallowed-tools: Read\n---\n"
        "# Ext\n!git diff\n",
        encoding="utf-8",
    )
    (e / "run.sh").write_text("#!/bin/sh\necho no\n", encoding="utf-8")
    # vendor-ish context fork
    v = root / ".claude" / "skills" / "vendorish"
    v.mkdir(parents=True)
    (v / "SKILL.md").write_text(
        "---\nname: vendorish\ndescription: Fork context\ncontext: fork\n---\nBody\n",
        encoding="utf-8",
    )
    # invalid
    inv = root / ".claude" / "skills" / "broken"
    inv.mkdir(parents=True)
    (inv / "SKILL.md").write_text("---\nname: broken\n---\nNo description\n", encoding="utf-8")
    # same name under another root
    c = root / ".cursor" / "skills" / "portable"
    c.mkdir(parents=True)
    (c / "SKILL.md").write_text(
        "---\nname: portable\ndescription: Cursor copy\n---\nCursor body\n",
        encoding="utf-8",
    )

    from conftest import override_projects

    override_projects(
        monkeypatch,
        {"demo": projects_mod.ProjectConfig(name="demo", root=str(root))},
    )
    return root


def test_list_metadata_only(skills_repo: Path) -> None:
    out = ProjectIntelligence(project="demo").skills_list()
    assert out["ok"]
    assert out["count"] >= 4
    for s in out["skills"]:
        assert "content" not in s
        assert "body" not in s


def test_bootstrap_includes_skills_not_deferred(skills_repo: Path) -> None:
    out = ProjectIntelligence(project="demo").bootstrap()
    assert out["ok"]
    assert out["skills"]
    assert "skills_discovery_deferred" not in out["warnings"]
    assert all("content" not in s for s in out["skills"])


def test_read_portable(skills_repo: Path) -> None:
    sid = ".claude/skills/portable/SKILL.md"
    out = ProjectIntelligence(project="demo").skill_read(sid)
    assert out["ok"]
    assert "Hello" in out["content"]
    assert out["skill"]["compatibility"] == "portable"
    rels = {f["relative"] for f in out["supporting_files"] if f.get("included")}
    assert ".claude/skills/portable/notes.txt" in rels


def test_bang_not_executed(skills_repo: Path) -> None:
    out = ProjectIntelligence(project="demo").skill_read(".claude/skills/extended/SKILL.md")
    assert out["ok"]
    assert "!git diff" in out["content"]
    assert "!command" in out["extensions_detected"]
    assert out["skill"]["compatibility"] == "portable_with_extensions"
    note = " ".join(out["compatibility_notes"])
    assert "never_executed" in note or "never_executed" in " ".join(
        out["skill"]["compatibility_notes"]
    )


def test_allowed_tools_metadata(skills_repo: Path) -> None:
    out = ProjectIntelligence(project="demo").skill_read(".claude/skills/extended/SKILL.md")
    assert any("allowed" in e for e in out["extensions_detected"])
    assert any("metadata_only" in n for n in out["compatibility_notes"])


def test_vendor_specific(skills_repo: Path) -> None:
    out = ProjectIntelligence(project="demo").skill_read(".claude/skills/vendorish/SKILL.md")
    assert out["ok"]
    assert out["skill"]["compatibility"] == "vendor_specific"


def test_invalid(skills_repo: Path) -> None:
    listed = ProjectIntelligence(project="demo").skills_list()
    broken = next(s for s in listed["skills"] if "broken" in s["skill_id"])
    assert broken["compatibility"] == "invalid"


def test_duplicate_names_distinct_ids(skills_repo: Path) -> None:
    out = ProjectIntelligence(project="demo").skills_list()
    ids = [s["skill_id"] for s in out["skills"] if s["name"] == "portable"]
    assert len(ids) == 2
    assert ".claude/skills/portable/SKILL.md" in ids
    assert ".cursor/skills/portable/SKILL.md" in ids


def test_bad_skill_id(skills_repo: Path) -> None:
    out = ProjectIntelligence(project="demo").skill_read("../etc/passwd")
    assert out["ok"] is False
    assert out["error"]["code"] in {"NOT_FOUND", "INVALID_ARGUMENT"}


def test_script_indexed_not_run(skills_repo: Path) -> None:
    out = ProjectIntelligence(project="demo").skill_read(".claude/skills/extended/SKILL.md")
    rels = [f["relative"] for f in out["supporting_files"]]
    assert any(r.endswith("run.sh") for r in rels)


def test_server_registers_skills(skills_repo: Path) -> None:
    import asyncio

    server = create_server(transport="stdio")

    async def _names() -> set[str]:
        return {t.name for t in await server.list_tools()}

    names = asyncio.run(_names())
    assert "project_skills_list" in names
    assert "project_skill_read" in names


def test_folded_description_gt_strip(skills_repo: Path) -> None:
    """YAML description: >- must unfold; not surface the literal '>-'."""
    root = Path(skills_repo)
    skill = root / ".claude" / "skills" / "folded"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: folded\n"
        "description: >-\n"
        "  First line of the skill.\n"
        "  Second line continues.\n"
        "---\n"
        "# Folded body\n",
        encoding="utf-8",
    )
    listed = ProjectIntelligence(project="demo").skills_list()
    row = next(s for s in listed["skills"] if s["skill_id"].endswith("folded/SKILL.md"))
    assert row["description"] == "First line of the skill. Second line continues."
    assert row["description"] != ">-"
    read = ProjectIntelligence(project="demo").skill_read(".claude/skills/folded/SKILL.md")
    assert read["ok"]
    assert read["skill"]["description"] == "First line of the skill. Second line continues."
