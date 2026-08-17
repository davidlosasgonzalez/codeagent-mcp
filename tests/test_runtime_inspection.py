"""Read-only runtime views: declared by the operator, confined, and off by default."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from codeagent_mcp.server import create_server
from codeagent_mcp.workspace.projects import _load_registry_from_path, get_project


def _write_registry(tmp_path: Path, entry: dict) -> Path:
    path = tmp_path / "projects.yaml"
    path.write_text(yaml.safe_dump({"projects": [entry]}), encoding="utf-8")
    return path


def test_registry_parses_runtime_paths(tmp_path: Path) -> None:
    path = _write_registry(
        tmp_path,
        {"id": "app", "root": str(tmp_path), "runtime_paths": {"data": "/var/lib/app/"}},
    )
    cfg = _load_registry_from_path(path)["app"]
    assert cfg.runtime_paths == {"data": "/var/lib/app"}


@pytest.mark.parametrize(
    "runtime_paths",
    [
        {"data": "relative/path"},
        {"data": "/etc"},
        {"data": "/etc/codeagent-mcp"},
        {"data": "/"},
        {"data": "/proc/self"},
        {"Data": "/var/lib/app"},
        {"": "/var/lib/app"},
    ],
)
def test_registry_refuses_bad_runtime_paths(tmp_path: Path, runtime_paths: dict) -> None:
    path = _write_registry(
        tmp_path, {"id": "app", "root": str(tmp_path), "runtime_paths": runtime_paths}
    )
    with pytest.raises(ValueError):
        _load_registry_from_path(path)


def test_registry_without_runtime_paths_defaults_to_empty(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, {"id": "app", "root": str(tmp_path)})
    assert _load_registry_from_path(path)["app"].runtime_paths == {}


def _tool_names(monkeypatch: pytest.MonkeyPatch, projects_file: Path) -> list[str]:
    monkeypatch.setenv("CODEAGENT_PROJECTS_FILE", str(projects_file))

    async def _run() -> list[str]:
        server = create_server(transport="stdio")
        return sorted(t.name for t in await server.list_tools())

    return asyncio.run(_run())


def test_tools_absent_when_no_project_declares_a_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_registry(tmp_path, {"id": "demo", "root": str(tmp_path)})
    names = _tool_names(monkeypatch, path)
    assert "runtime_list" not in names
    assert "runtime_read" not in names


def test_tools_appear_when_a_project_declares_a_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_registry(
        tmp_path,
        {"id": "demo", "root": str(tmp_path), "runtime_paths": {"data": str(tmp_path / "live")}},
    )
    names = _tool_names(monkeypatch, path)
    assert "runtime_list" in names
    assert "runtime_read" in names


@pytest.fixture()
def live_view(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A registry whose demo project exposes one read-only view holding a file."""
    live = tmp_path / "live"
    (live / "sub").mkdir(parents=True)
    (live / "units.json").write_text('{"units": 3}\n', encoding="utf-8")
    (tmp_path / "outside.txt").write_text("must stay unreachable\n", encoding="utf-8")
    path = _write_registry(
        tmp_path,
        {"id": "demo", "root": str(tmp_path / "repo"), "runtime_paths": {"data": str(live)}},
    )
    (tmp_path / "repo").mkdir()
    monkeypatch.setenv("CODEAGENT_PROJECTS_FILE", str(path))
    return live


def _call(tool_name: str, **kwargs) -> dict:
    """Invoke a runtime tool through the live server (kwargs are the tool's own args)."""
    from codeagent_mcp.tools import runtime as runtime_mod

    async def _run() -> dict:
        server = create_server(transport="stdio")
        tool = await server.get_tool(tool_name)
        return await tool.run(kwargs)  # type: ignore[no-any-return]

    assert runtime_mod.projects_with_runtime_paths() == ("demo",)
    result = asyncio.run(_run())
    return result.structured_content or {}


def test_runtime_list_without_a_name_reports_declared_views(live_view: Path) -> None:
    out = _call("runtime_list", project="demo")
    assert out["ok"] is True
    assert out["views"] == [{"name": "data", "path": str(live_view)}]


def test_runtime_list_lists_inside_the_view(live_view: Path) -> None:
    out = _call("runtime_list", project="demo", name="data")
    assert out["ok"] is True
    assert {e["name"] for e in out["entries"]} == {"units.json", "sub"}
    assert out["view"] == "data"


def test_runtime_read_returns_file_content(live_view: Path) -> None:
    out = _call("runtime_read", project="demo", name="data", path="units.json")
    assert out["ok"] is True
    assert '"units": 3' in out["content"]


def test_runtime_read_cannot_escape_the_view(live_view: Path) -> None:
    out = _call("runtime_read", project="demo", name="data", path="../outside.txt")
    assert out["ok"] is False
    assert out["error"]["code"] in {"PATH_OUTSIDE_ROOT", "NOT_FOUND", "INVALID_ARGUMENT"}


def test_unknown_view_name_is_named_as_such(live_view: Path) -> None:
    out = _call("runtime_read", project="demo", name="nope", path="units.json")
    assert out["ok"] is False
    assert out["error"]["code"] == "NOT_FOUND"


def test_view_is_not_the_project_checkout(live_view: Path) -> None:
    """A view is its own jail — it must not reach the project root."""
    cfg = get_project("demo")
    assert cfg is not None
    assert cfg.root != str(live_view)
    out = _call("runtime_list", project="demo", name="data", path=cfg.root)
    assert out["ok"] is False
