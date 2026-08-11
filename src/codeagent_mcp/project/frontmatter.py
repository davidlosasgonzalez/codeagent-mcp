"""YAML frontmatter parser for .mdc / markdown (untrusted repo content)."""

from __future__ import annotations

import re
from typing import Any

import yaml

# Cap frontmatter block size to limit YAML DoS from untrusted skills/rules.
_MAX_FRONTMATTER_BYTES = 64_000


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (meta, body). If no frontmatter, meta is empty and body is full text."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta_lines: list[str] = []
    i = 1
    while i < len(lines):
        if lines[i].strip() == "---":
            body = "".join(lines[i + 1 :])
            return _parse_yaml_meta("".join(meta_lines)), body
        meta_lines.append(lines[i])
        i += 1
    return {}, text


def _parse_yaml_meta(block: str) -> dict[str, Any]:
    """Parse frontmatter with SafeLoader. Fail closed on errors / non-mappings."""
    if not block.strip():
        return {}
    if len(block.encode("utf-8", errors="replace")) > _MAX_FRONTMATTER_BYTES:
        return {}
    try:
        data = yaml.safe_load(block)
    except (yaml.YAMLError, RecursionError, MemoryError):
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items()}


def expand_glob_braces(pattern: str) -> list[str]:
    """Expand one-level {a,b} braces for matching."""
    m = re.search(r"\{([^{}]+)\}", pattern)
    if not m:
        return [pattern]
    options = [o.strip() for o in m.group(1).split(",")]
    prefix = pattern[: m.start()]
    suffix = pattern[m.end() :]
    out: list[str] = []
    for opt in options:
        out.extend(expand_glob_braces(prefix + opt + suffix))
    return out
