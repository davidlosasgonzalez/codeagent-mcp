"""Read-only git_status / git_diff against a registered project root."""

from __future__ import annotations

import os
import subprocess
from typing import Any, Literal

from codeagent_mcp.errors import tool_error, tool_ok
from codeagent_mcp.paths import resolve_under_root
from codeagent_mcp.workspace.projects import get_project, known_projects

DEFAULT_MAX_DIFF_BYTES = 100_000
HARD_MAX_DIFF_BYTES = 1_000_000
DEFAULT_MAX_ENTRIES = 500
HARD_MAX_ENTRIES = 2000
GIT_TIMEOUT_S = 30

DiffMode = Literal["unstaged", "staged", "both"]


def run_git(
    root: str,
    args: list[str],
    *,
    extra_env: dict[str, str] | None = None,
    timeout_s: float = GIT_TIMEOUT_S,
) -> subprocess.CompletedProcess[str]:
    # Worktrees / mixed root ownership trip Git "dubious ownership" for codeagent-mcp.
    # Scope the exception to this invocation only (not a system-wide safe.directory).
    return subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={root}",
            "-c",
            "safe.directory=*",
            "-C",
            root,
            "--no-optional-locks",
            *args,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env={
            **os.environ,
            "LC_ALL": "C",
            "GIT_TERMINAL_PROMPT": "0",
            **(extra_env or {}),
        },
    )


def ensure_repo(root: str) -> dict[str, Any] | None:
    proc = run_git(root, ["rev-parse", "--is-inside-work-tree"])
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        return tool_error(
            "NOT_A_GIT_REPO",
            f"project root is not a usable git work tree: {root}"
            + (f" ({detail})" if detail else ""),
            retryable=False,
        )
    return None


def resolve_pathspec(root: str, path: str) -> tuple[str | None, dict[str, Any] | None]:
    if not path or not path.strip():
        return None, None
    try:
        resolved = resolve_under_root(path.strip(), root)
    except (OSError, ValueError) as exc:
        return None, tool_error(
            "PATH_OUTSIDE_ROOT",
            str(exc),
            retryable=False,
        )
    rel = os.path.relpath(resolved, root)
    if rel.startswith(".."):
        return None, tool_error(
            "PATH_OUTSIDE_ROOT",
            f"path escapes project root: {path!r}",
            retryable=False,
        )
    return rel if rel != "." else None, None


def git_status(
    *,
    project: str = "demo",
    path: str = "",
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> dict[str, Any]:
    cfg = get_project(project)
    if cfg is None:
        return tool_error(
            "UNKNOWN_PROJECT",
            f"unknown project {project!r}; known={list(known_projects())}",
            retryable=False,
        )
    root = cfg.root
    err = ensure_repo(root)
    if err is not None:
        return err

    max_entries = max(1, min(int(max_entries), HARD_MAX_ENTRIES))
    pathspec, perr = resolve_pathspec(root, path)
    if perr is not None:
        return perr

    args = ["status", "--porcelain=v2", "-b", "--untracked-files=all"]
    if pathspec:
        args.extend(["--", pathspec])
    proc = run_git(root, args)
    if proc.returncode != 0:
        return tool_error(
            "GIT_FAILED",
            (proc.stderr or proc.stdout or "git status failed").strip()[:500],
            retryable=True,
            next_action="Retry or use exec_run for diagnostics",
        )

    branch: str | None = None
    detached = False
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None
    staged: list[dict[str, str]] = []
    unstaged: list[dict[str, str]] = []
    untracked: list[dict[str, str]] = []
    trunc = {"staged": False, "unstaged": False, "untracked": False}

    for line in proc.stdout.splitlines():
        if line.startswith("# branch.head "):
            name = line[len("# branch.head ") :].strip()
            if name == "(detached)":
                detached = True
                branch = None
            else:
                branch = name
            continue
        if line.startswith("# branch.upstream "):
            upstream = line[len("# branch.upstream ") :].strip()
            continue
        if line.startswith("# branch.ab "):
            # # branch.ab +<ahead> -<behind>
            parts = line.split()
            for p in parts[2:]:
                if p.startswith("+"):
                    ahead = int(p[1:])
                elif p.startswith("-"):
                    behind = int(p[1:])
            continue
        if line.startswith("# "):
            continue
        if line.startswith("1 ") or line.startswith("2 "):
            # ordinary / rename
            parts = line.split(" ", 8)
            if len(parts) < 9:
                continue
            xy = parts[1]
            path_field = parts[8]
            if "\t" in path_field:
                # rename: old\tnew
                path_field = path_field.split("\t")[-1]
            x, y = xy[0], xy[1]
            if x != "." and x != " ":
                if len(staged) < max_entries:
                    staged.append({"path": path_field, "status": x})
                else:
                    trunc["staged"] = True
            if y != "." and y != " ":
                if len(unstaged) < max_entries:
                    unstaged.append({"path": path_field, "status": y})
                else:
                    trunc["unstaged"] = True
            continue
        if line.startswith("? "):
            upath = line[2:]
            if len(untracked) < max_entries:
                untracked.append({"path": upath, "status": "?"})
            else:
                trunc["untracked"] = True
            continue
        if line.startswith("! "):
            # ignored — skip
            continue

    clean = not staged and not unstaged and not untracked
    return tool_ok(
        project=cfg.name,
        root=root,
        branch=branch,
        detached=detached,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        clean=clean,
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
        truncated=trunc,
        counts={
            "staged": len(staged),
            "unstaged": len(unstaged),
            "untracked": len(untracked),
        },
        max_entries=max_entries,
    )


def git_diff(
    *,
    project: str = "demo",
    path: str = "",
    mode: DiffMode = "both",
    max_bytes: int = DEFAULT_MAX_DIFF_BYTES,
) -> dict[str, Any]:
    cfg = get_project(project)
    if cfg is None:
        return tool_error(
            "UNKNOWN_PROJECT",
            f"unknown project {project!r}; known={list(known_projects())}",
            retryable=False,
        )
    root = cfg.root
    err = ensure_repo(root)
    if err is not None:
        return err

    max_bytes = max(1024, min(int(max_bytes), HARD_MAX_DIFF_BYTES))
    if mode not in ("unstaged", "staged", "both"):
        return tool_error(
            "INVALID_ARGUMENT",
            "mode must be unstaged|staged|both",
            retryable=False,
        )

    pathspec, perr = resolve_pathspec(root, path)
    if perr is not None:
        return perr

    chunks: list[str] = []
    file_rows: list[dict[str, Any]] = []
    total_ins = 0
    total_del = 0

    def _numstat(staged: bool) -> None:
        nonlocal total_ins, total_del
        args = ["diff", "--numstat"]
        if staged:
            args.append("--cached")
        if pathspec:
            args.extend(["--", pathspec])
        proc = run_git(root, args)
        if proc.returncode != 0:
            return
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            a, d, pth = parts
            ins = 0 if a == "-" else int(a)
            dele = 0 if d == "-" else int(d)
            total_ins += ins
            total_del += dele
            file_rows.append(
                {
                    "path": pth,
                    "scope": "staged" if staged else "unstaged",
                    "insertions": ins,
                    "deletions": dele,
                }
            )

    def _patch(staged: bool) -> str:
        args = ["diff", "--no-color", "--find-renames"]
        if staged:
            args.append("--cached")
        if pathspec:
            args.extend(["--", pathspec])
        proc = run_git(root, args)
        if proc.returncode not in (0, 1):
            return ""
        return proc.stdout or ""

    if mode in ("staged", "both"):
        _numstat(True)
        chunks.append(_patch(True))
    if mode in ("unstaged", "both"):
        _numstat(False)
        chunks.append(_patch(False))

    blob = "".join(chunks)
    truncated = False
    if len(blob.encode("utf-8")) > max_bytes:
        # truncate on byte boundary
        raw = blob.encode("utf-8")[:max_bytes]
        blob = raw.decode("utf-8", errors="ignore")
        truncated = True

    return tool_ok(
        project=cfg.name,
        root=root,
        mode=mode,
        path=pathspec or "",
        summary={
            "files": len(file_rows),
            "insertions": total_ins,
            "deletions": total_del,
        },
        files=file_rows[:DEFAULT_MAX_ENTRIES],
        diff=blob,
        truncated=truncated,
        bytes=len(blob.encode("utf-8")),
        max_bytes=max_bytes,
    )
