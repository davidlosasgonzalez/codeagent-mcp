"""Path confinement helpers for cwd/roots."""

from __future__ import annotations

from pathlib import Path


def resolve_under_root(path: str | Path, root: str | Path) -> Path:
    """Resolve ``path`` and require it stays under ``root`` after symlink resolution.

    Raises:
        ValueError: if the resolved path escapes ``root``.
    """
    root_resolved = Path(root).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root_resolved / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"path {resolved} is outside authorized root {root_resolved}") from exc
    return resolved
