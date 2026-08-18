"""runtime_tail: the end of a file, which is where a growing log keeps its news.

runtime_read starts at the beginning, so on a log that has been appended to all
month a byte-capped read returns the first day and never reaches what anyone is
asking about. These tests pin that the tail is the tail, and that walking
backwards does not walk out of the view.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from codeagent_mcp.server import create_server


@pytest.fixture()
def view(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    live = tmp_path / "live"
    live.mkdir()
    # Comfortably larger than the 64 KiB read step, so the backwards walk has to
    # take more than one bite.
    (live / "big.jsonl").write_text(
        "".join(f'{{"n":{i},"pad":"{"x" * 200}"}}\n' for i in range(2000)),
        encoding="utf-8",
    )
    (live / "small.log").write_text("uno\ndos\ntres\n", encoding="utf-8")
    (live / "no-newline.log").write_text("solo una linea sin salto", encoding="utf-8")
    (live / "empty.log").write_text("", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("no", encoding="utf-8")

    (tmp_path / "repo").mkdir()
    path = tmp_path / "projects.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "projects": [
                    {
                        "id": "demo",
                        "root": str(tmp_path / "repo"),
                        "runtime_paths": {"data": str(live)},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEAGENT_PROJECTS_FILE", str(path))
    return live


def _call(tool_name: str, **kwargs: Any) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        server = create_server(transport="stdio")
        result = await server.call_tool(tool_name, kwargs)
        assert result.structured_content is not None
        return result.structured_content

    return asyncio.run(_run())


def test_returns_the_last_lines_not_the_first(view: Path) -> None:
    out = _call("runtime_tail", project="demo", name="data", path="big.jsonl", lines=3)
    assert out["ok"] is True
    lines = out["content"].splitlines()
    assert len(lines) == 3
    assert '"n":1999' in lines[-1]
    assert '"n":1997' in lines[0]
    assert '"n":0' not in out["content"], "this is exactly what runtime_read would give"


def test_reports_that_it_holds_only_the_end(view: Path) -> None:
    out = _call("runtime_tail", project="demo", name="data", path="big.jsonl", lines=3)
    assert out["truncated"] is True
    assert out["size_bytes"] > 400_000
    assert out["lines_returned"] == 3


def test_a_short_file_comes_back_whole_and_untruncated(view: Path) -> None:
    out = _call("runtime_tail", project="demo", name="data", path="small.log", lines=100)
    assert out["ok"] is True
    assert out["content"] == "uno\ndos\ntres"
    assert out["lines_returned"] == 3
    assert out["truncated"] is False


def test_a_file_without_a_trailing_newline_is_not_lost(view: Path) -> None:
    out = _call("runtime_tail", project="demo", name="data", path="no-newline.log")
    assert out["ok"] is True
    assert out["content"] == "solo una linea sin salto"


def test_an_empty_file_is_empty_not_an_error(view: Path) -> None:
    out = _call("runtime_tail", project="demo", name="data", path="empty.log")
    assert out["ok"] is True
    assert out["content"] == ""
    assert out["lines_returned"] == 0


def test_line_count_is_capped(view: Path) -> None:
    out = _call("runtime_tail", project="demo", name="data", path="big.jsonl", lines=10_000_000)
    assert out["ok"] is True
    assert out["lines_returned"] <= 5000


def test_it_cannot_walk_out_of_the_view(view: Path) -> None:
    out = _call("runtime_tail", project="demo", name="data", path="../outside.txt")
    assert out["ok"] is False
    assert out["error"]["code"] in {"PATH_OUTSIDE_ROOT", "NOT_FOUND", "INVALID_ARGUMENT"}


def test_an_undeclared_view_is_refused(view: Path) -> None:
    out = _call("runtime_tail", project="demo", name="secrets", path="small.log")
    assert out["ok"] is False
    assert out["error"]["code"] == "NOT_FOUND"
