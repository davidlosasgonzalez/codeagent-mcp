"""Project registry gates (YAML-backed)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codeagent_mcp.workspace import projects as projects_mod
from codeagent_mcp.workspace.projects import get_project, known_projects


@pytest.fixture()
def sample_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "projects.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "projects": [
                    {
                        "id": "app",
                        "root": "/srv/app",
                        "writable_env": "CODEAGENT_APP_WRITE",
                    },
                    {
                        "id": "app-prod",
                        "root": "/srv/app",
                        "writable_env": "CODEAGENT_APP_WRITE",
                    },
                    {
                        "id": "worker",
                        "root": "/srv/worker",
                        "writable_env": "CODEAGENT_WORKER_WRITE",
                    },
                    {
                        "id": "worker-prod",
                        "root": "/srv/worker",
                        "writable_env": "CODEAGENT_WORKER_WRITE",
                    },
                    {
                        "id": "bench",
                        "root": "/var/lib/codeagent-mcp/bench",
                        "writable": True,
                        "env": {"MYAPP_DATA_ROOT": "/var/lib/codeagent-mcp/bench-data"},
                    },
                    {
                        "id": "demo",
                        "root": "/var/lib/codeagent-mcp/demo-root",
                        "writable": True,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEAGENT_PROJECTS_FILE", str(path))
    return path


def test_known_lists_every_registered_id(sample_projects: Path) -> None:
    del sample_projects
    names = known_projects()
    assert "app" in names and "app-prod" in names
    assert "worker" in names and "worker-prod" in names
    assert "demo" in names and "bench" in names


def test_root_and_write_flag(sample_projects: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del sample_projects
    monkeypatch.delenv("CODEAGENT_WORKER_WRITE", raising=False)
    cfg = get_project("worker-prod")
    assert cfg is not None
    assert cfg.root == "/srv/worker"
    assert cfg.writable is False
    monkeypatch.setenv("CODEAGENT_WORKER_WRITE", "1")
    cfg2 = get_project("worker")
    assert cfg2 is not None and cfg2.writable is True


def test_write_flags_are_independent(
    sample_projects: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del sample_projects
    monkeypatch.setenv("CODEAGENT_APP_WRITE", "0")
    monkeypatch.setenv("CODEAGENT_WORKER_WRITE", "1")
    app_prod = get_project("app-prod")
    worker_prod = get_project("worker-prod")
    assert app_prod is not None and worker_prod is not None
    assert app_prod.writable is False
    assert worker_prod.writable is True
    monkeypatch.setenv("CODEAGENT_APP_WRITE", "1")
    app = get_project("app")
    assert app is not None and app.writable is True


def test_project_env_is_parsed(sample_projects: Path) -> None:
    del sample_projects
    bench = get_project("bench")
    assert bench is not None
    assert bench.env == {"MYAPP_DATA_ROOT": "/var/lib/codeagent-mcp/bench-data"}
    assert get_project("demo") is not None
    assert get_project("demo").env == {}  # type: ignore[union-attr]


def test_project_env_rejects_reserved_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "projects.yaml"
    path.write_text(
        yaml.safe_dump(
            {"projects": [{"id": "bad", "root": "/srv/bad", "env": {"LD_PRELOAD": "/evil.so"}}]}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEAGENT_PROJECTS_FILE", str(path))
    with pytest.raises(ValueError, match="not allowed"):
        known_projects()


def test_example_file_loads_demo_only(monkeypatch: pytest.MonkeyPatch) -> None:
    example = Path(projects_mod.__file__).resolve().parents[3] / "deploy" / "projects.example.yaml"
    monkeypatch.setenv("CODEAGENT_PROJECTS_FILE", str(example))
    names = known_projects()
    assert names == ("demo",)
    assert get_project("demo") is not None
    assert get_project("app") is None
