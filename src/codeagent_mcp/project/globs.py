"""Path glob matching for Cursor-style globs."""

from __future__ import annotations

from pathlib import PurePosixPath

from codeagent_mcp.project.frontmatter import expand_glob_braces


def path_matches_any(rel_path: str, patterns: list[str] | str | None) -> bool:
    if not patterns:
        return False
    if isinstance(patterns, str):
        patterns = [patterns]
    rel = rel_path.replace("\\", "/").lstrip("./")
    target = PurePosixPath(rel)
    for pattern in patterns:
        for expanded in expand_glob_braces(pattern):
            exp = expanded.replace("\\", "/")
            try:
                if target.match(exp):
                    return True
            except ValueError:
                continue
            # also try matching with leading **/
            if not exp.startswith("**/") and target.match("**/" + exp):
                return True
    return False
