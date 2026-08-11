"""Shared pytest helpers for CodeAgent MCP."""

from __future__ import annotations

from collections.abc import Mapping

import pytest
import yaml

from codeagent_mcp.workspace import projects as projects_mod
from codeagent_mcp.workspace.projects import ProjectConfig


@pytest.fixture(autouse=True)
def _default_projects_file(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
):
    """Isolate tests from the host's projects.yaml."""
    path = tmp_path_factory.mktemp("projcfg") / "projects.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "projects": [
                    {
                        "id": "demo",
                        "root": "/var/lib/codeagent-mcp/demo-root",
                        "writable": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEAGENT_PROJECTS_FILE", str(path))


def override_projects(
    monkeypatch: pytest.MonkeyPatch,
    overrides: Mapping[str, ProjectConfig],
) -> None:
    """Replace server project registry entries for the duration of a test."""
    custom = dict(overrides)
    monkeypatch.setattr(projects_mod, "_registry", lambda: custom)
