"""Worktree baselines: what the checkout looked like when a lease was acquired.

``git_status`` and ``git_diff`` answer "what is dirty", which is not the question
a caller arriving at an already-dirty checkout actually has. It needs "what did
*I* change", and nothing in staged/unstaged separates its own edits from work
that was there before it connected.

So the lease records a baseline: a git tree object written at acquire time.
``diff_since`` writes a second tree from the current worktree and diffs the two,
which is exact regardless of what happened in between — staging, unstaging, even
commits.

The snapshot writes blob and tree objects into the repository's object store
using a **temporary index file**. It never touches HEAD, refs, the real index or
the worktree; unreferenced objects are reclaimed by the repository's own ``gc``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codeagent_mcp.errors import tool_error, tool_ok
from codeagent_mcp.git.service import (
    DEFAULT_MAX_DIFF_BYTES,
    DEFAULT_MAX_ENTRIES,
    HARD_MAX_DIFF_BYTES,
    ensure_repo,
    resolve_pathspec,
    run_git,
)

# Hashing a large worktree costs real time; the temp index is seeded from the
# repository's own index so only files that actually differ get re-hashed.
SNAPSHOT_TIMEOUT_S = 180


class BaselineError(RuntimeError):
    """The worktree could not be snapshotted (repo missing, unwritable, timeout)."""


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _real_index_path(root: str) -> Path | None:
    proc = run_git(root, ["rev-parse", "--git-path", "index"])
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path(root) / path
    return path if path.is_file() else None


def _head_sha(root: str) -> str:
    proc = run_git(root, ["rev-parse", "HEAD"])
    return proc.stdout.strip() if proc.returncode == 0 else ""


def snapshot(root: str) -> dict[str, str]:
    """Record the current worktree as a git tree object.

    Returns a baseline mapping with ``tree``, ``head`` and ``taken_at``.

    Raises:
        BaselineError: the root is not a usable work tree, or git refused.
    """
    if ensure_repo(root) is not None:
        raise BaselineError(f"not a git work tree: {root}")

    with tempfile.TemporaryDirectory(prefix="codeagent-baseline-") as tmp:
        index_path = Path(tmp) / "index"
        real_index = _real_index_path(root)
        if real_index is not None:
            try:
                shutil.copy2(real_index, index_path)
            except OSError:
                # A missing or unreadable index only costs a full re-hash.
                pass

        env_index = str(index_path)
        add = run_git(
            root,
            ["add", "--all", "--"],
            extra_env={"GIT_INDEX_FILE": env_index},
            timeout_s=SNAPSHOT_TIMEOUT_S,
        )
        if add.returncode != 0:
            raise BaselineError((add.stderr or add.stdout or "git add failed").strip()[:300])

        write = run_git(
            root,
            ["write-tree"],
            extra_env={"GIT_INDEX_FILE": env_index},
            timeout_s=SNAPSHOT_TIMEOUT_S,
        )
        if write.returncode != 0:
            raise BaselineError((write.stderr or write.stdout or "write-tree failed").strip()[:300])
        tree = write.stdout.strip()

    if not tree:
        raise BaselineError("git write-tree returned no object id")
    return {"tree": tree, "head": _head_sha(root), "taken_at": _iso_now()}


def diff_since(
    *,
    project: str,
    root: str,
    baseline: dict[str, Any] | None,
    path: str = "",
    max_bytes: int = DEFAULT_MAX_DIFF_BYTES,
) -> dict[str, Any]:
    """Diff the worktree as it stands now against the lease's acquire-time tree."""
    err = ensure_repo(root)
    if err is not None:
        return err

    tree = str((baseline or {}).get("tree") or "")
    if not tree:
        return tool_error(
            "BASELINE_UNAVAILABLE",
            "this lease carries no acquire-time baseline",
            retryable=False,
            next_action=(
                "Release and re-acquire the lease to record one, or use git_diff "
                "if the whole dirty tree is what you need"
            ),
            project=project,
            root=root,
        )

    max_bytes = max(1024, min(int(max_bytes), HARD_MAX_DIFF_BYTES))
    pathspec, perr = resolve_pathspec(root, path)
    if perr is not None:
        return perr

    try:
        current = snapshot(root)
    except BaselineError as exc:
        return tool_error(
            "GIT_FAILED",
            f"could not snapshot the current worktree: {exc}",
            retryable=True,
            next_action="Check the repository is readable and its object store writable",
            project=project,
            root=root,
        )

    def _tree_diff(args: list[str]) -> tuple[int, str]:
        cmd = ["diff-tree", "--find-renames", "-r", *args, tree, current["tree"]]
        if pathspec:
            cmd.extend(["--", pathspec])
        proc = run_git(root, cmd, timeout_s=SNAPSHOT_TIMEOUT_S)
        return proc.returncode, proc.stdout or ""

    rc, numstat_out = _tree_diff(["--numstat"])
    if rc not in (0, 1):
        return tool_error(
            "GIT_FAILED",
            "git diff-tree --numstat failed",
            retryable=True,
            project=project,
            root=root,
        )

    files: list[dict[str, Any]] = []
    total_ins = 0
    total_del = 0
    for line in numstat_out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, removed, pth = parts[0], parts[1], parts[-1]
        ins = 0 if added == "-" else int(added)
        dele = 0 if removed == "-" else int(removed)
        total_ins += ins
        total_del += dele
        files.append(
            {
                "path": pth,
                "scope": "since_acquire",
                "insertions": ins,
                "deletions": dele,
                "binary": added == "-" and removed == "-",
            }
        )

    rc, blob = _tree_diff(["--patch", "--no-color"])
    if rc not in (0, 1):
        blob = ""
    truncated = False
    if len(blob.encode("utf-8")) > max_bytes:
        blob = blob.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        truncated = True

    baseline_head = str((baseline or {}).get("head") or "")
    return tool_ok(
        project=project,
        root=root,
        path=pathspec or "",
        baseline={
            "tree": tree,
            "head": baseline_head,
            "taken_at": (baseline or {}).get("taken_at", ""),
        },
        current={"tree": current["tree"], "head": current["head"]},
        head_moved=bool(baseline_head) and baseline_head != current["head"],
        summary={
            "files": len(files),
            "insertions": total_ins,
            "deletions": total_del,
        },
        files=files[:DEFAULT_MAX_ENTRIES],
        diff=blob,
        truncated=truncated,
        bytes=len(blob.encode("utf-8")),
        max_bytes=max_bytes,
        note=(
            "Changes made since this lease was acquired, whatever their staging state. "
            "Pre-existing dirt in the checkout is excluded by construction."
        ),
    )


def baseline_enabled() -> bool:
    """Operators can switch acquire-time snapshots off for very large checkouts."""
    return os.environ.get("CODEAGENT_LEASE_BASELINE", "1").strip() != "0"
