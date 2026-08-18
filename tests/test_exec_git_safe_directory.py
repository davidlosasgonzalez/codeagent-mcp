"""git must work through exec_run on a checkout the service account does not own.

Registered roots routinely belong to another account, with the service reaching
them through a group. Git calls that "dubious ownership" and refuses the whole
repository — so git_status read the checkout happily while every git command run
through exec_run failed on it, which reads as "exec_run is broken", not as a
permissions policy.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codeagent_mcp.exec.env import apply_git_safe_directory, merge_env_overrides


@pytest.fixture(autouse=True)
def _private_tmpdir(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Give every test here its own temp root.

    merge_env_overrides pins TMPDIR into the child environment and creates the
    directory. Without an override that is the production path, which exists on
    the deployment host and cannot be created anywhere else — so these tests
    passed on one machine and failed on every other.
    """
    monkeypatch.setenv("TMPDIR", str(tmp_path_factory.mktemp("svc-tmp")))


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


def test_names_the_root_literally_never_a_wildcard(tmp_path: Path) -> None:
    env = apply_git_safe_directory({}, str(tmp_path))
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert env["GIT_CONFIG_VALUE_0"] == str(tmp_path)
    assert "*" not in env["GIT_CONFIG_VALUE_0"]


def test_leaves_the_rest_of_the_environment_alone(tmp_path: Path) -> None:
    base = {"PATH": "/usr/bin", "TMPDIR": "/somewhere"}
    env = apply_git_safe_directory(base, str(tmp_path))
    assert env["PATH"] == "/usr/bin"
    assert env["TMPDIR"] == "/somewhere"
    assert base == {"PATH": "/usr/bin", "TMPDIR": "/somewhere"}, "must not mutate the input"


def test_git_runs_in_a_repo_owned_by_someone_else(tmp_path: Path) -> None:
    """The regression itself, reproduced without needing a second uid.

    GIT_CEILING_DIRECTORIES is not the mechanism under test; ownership is. The
    check is that a real git invocation carrying our env succeeds on the root.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "f.txt").write_text("x\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")

    env = merge_env_overrides(None)
    assert isinstance(env, dict) and env.get("ok") is not False
    env = apply_git_safe_directory(env, str(root))  # type: ignore[arg-type]

    proc = subprocess.run(
        ["git", "-C", str(root), "diff", "--check"],
        capture_output=True,
        text=True,
        check=False,
        env=env,  # type: ignore[arg-type]
    )
    assert proc.returncode == 0, proc.stderr
    assert "dubious ownership" not in proc.stderr


def test_exec_run_injects_it_after_project_env(tmp_path: Path) -> None:
    """A project's own env map must not be able to shadow the exception."""
    from codeagent_mcp.exec.env import apply_project_env

    # TMPDIR goes in the mapping, not the process environment: this helper
    # resolves the temp root from what it is handed, and without it the
    # production default is used and cannot be created off-host.
    env = apply_project_env(
        {"PATH": "/usr/bin", "TMPDIR": str(tmp_path / "svc-tmp")},
        {"GIT_CONFIG_COUNT": "9"},
    )
    # The project map is applied first; exec_run then overwrites it.
    env = apply_git_safe_directory(env, str(tmp_path))
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_VALUE_0"] == str(tmp_path)
