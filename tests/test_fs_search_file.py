"""fs_search with a file as path.

Two defects, one after the other. First the jail insisted on a directory, so a
file was rejected outright. Then the file was accepted and returned **zero
matches**: given one explicit file, ripgrep drops the path column, and the match
parser read the first field as a path and discarded every line.

The second one is the reason these tests assert on counts and not on `ok`. An
empty result set looked exactly like a clean answer, which is how it survived a
round of "fixed".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeagent_mcp.fs.service import FsService

CONTENT = """def alpha():
    return 1


def beta():
    return 2


class Gamma:
    def delta(self):
        return 3
"""

OTHER = """def elsewhere():
    return 4
"""


@pytest.fixture()
def project(tmp_path: Path) -> FsService:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text(CONTENT, encoding="utf-8")
    (root / "pkg" / "other.py").write_text(OTHER, encoding="utf-8")
    (tmp_path / "outside.py").write_text("def secret(): pass\n", encoding="utf-8")
    return FsService(project="demo", root=str(root))


def test_a_file_path_returns_that_file_matches(project: FsService) -> None:
    """Two functions and one method, on the lines they are actually written on."""
    out = project.search("def ", path="pkg/mod.py")
    assert out["ok"] is True
    assert {m["line"] for m in out["matches"]} == {1, 5, 10}


def test_a_file_path_does_not_leak_its_neighbours(project: FsService) -> None:
    out = project.search("elsewhere", path="pkg/mod.py")
    assert out["ok"] is True
    assert out["matches"] == [], "other.py is next to it and must not be searched"


def test_the_same_query_still_works_on_a_directory(project: FsService) -> None:
    out = project.search("def ", path="pkg")
    assert out["ok"] is True
    files = {m["relative"].split("/")[-1] for m in out["matches"]}
    assert files == {"mod.py", "other.py"}


def test_matches_carry_a_usable_path_and_line(project: FsService) -> None:
    """The regression was a parser that silently dropped every row."""
    out = project.search("beta", path="pkg/mod.py")
    assert out["ok"] is True
    assert len(out["matches"]) == 1
    match = out["matches"][0]
    assert match["line"] == 5
    assert match["relative"].endswith("mod.py")
    assert Path(match["path"]).is_file()
    assert "def beta" in match["text"]


def test_a_file_outside_the_root_is_refused(project: FsService) -> None:
    out = project.search("secret", path="../outside.py")
    assert out["ok"] is False
    assert out["error"]["code"] in {"PATH_OUTSIDE_ROOT", "NOT_FOUND", "INVALID_ARGUMENT"}


def test_a_missing_file_is_reported(project: FsService) -> None:
    out = project.search("anything", path="pkg/absent.py")
    assert out["ok"] is False
