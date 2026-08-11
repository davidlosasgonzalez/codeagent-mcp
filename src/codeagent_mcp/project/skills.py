"""Agent Skills discovery and read. Never executes skill content."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from codeagent_mcp.fs.openat2 import JailError, PathJail
from codeagent_mcp.project.frontmatter import parse_frontmatter

SKILL_ROOTS = (
    ".claude/skills",
    ".cursor/skills",
    ".agents/skills",
    ".codex/skills",
)

Compatibility = Literal[
    "portable",
    "portable_with_extensions",
    "vendor_specific",
    "invalid",
]

# Frontmatter keys that are still portable-with-extensions (metadata only)
_EXTENSION_KEYS = frozenset(
    {
        "allowed-tools",
        "allowed_tools",
        "disable-model-invocation",
        "disable_model_invocation",
        "user-invocable",
        "user_invocable",
        "context",
        "agent",
        "model",
    }
)

# Keys that make the skill depend on vendor runtime semantics
_VENDOR_KEYS = frozenset(
    {
        "context",  # e.g. fork
    }
)

_BANG_COMMAND = re.compile(r"(?m)^\s*!\s*[A-Za-z0-9_./-]+")


@dataclass(slots=True)
class SkillDoc:
    skill_id: str
    name: str
    description: str
    origin: str
    relative_dir: str
    relative_skill_md: str
    path: str
    sha256: str
    compatibility: Compatibility
    extensions_detected: list[str] = field(default_factory=list)
    compatibility_notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    body: str = ""
    supporting_files: list[dict[str, Any]] = field(default_factory=list)

    def manifest(self, *, description_max: int = 400) -> dict[str, Any]:
        desc = self.description
        truncated = False
        if len(desc) > description_max:
            desc = desc[:description_max]
            truncated = True
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": desc,
            "description_truncated": truncated,
            "origin": self.origin,
            "relative_dir": self.relative_dir,
            "compatibility": self.compatibility,
            "extensions_detected": list(self.extensions_detected),
            "compatibility_notes": list(self.compatibility_notes),
            "warnings": list(self.warnings),
            "sha256": self.sha256,
        }


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(jail: PathJail, rel: str, *, max_load: int = 2_000_000) -> bytes:
    fd = jail.open(rel)
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            if total < max_load:
                need = max_load - total
                chunks.append(block[:need])
                total += len(block[:need])
        return b"".join(chunks)
    finally:
        os.close(fd)


def classify_skill(meta: dict[str, Any], body: str) -> tuple[Compatibility, list[str], list[str]]:
    """Return compatibility, extensions_detected, notes. Never executes anything."""
    extensions: list[str] = []
    notes: list[str] = []
    name = meta.get("name")
    description = meta.get("description")
    if not isinstance(name, str) or not name.strip():
        return "invalid", extensions, ["missing_name"]
    if not isinstance(description, str) or not description.strip():
        return "invalid", extensions, ["missing_description"]

    vendorish = False
    for key in meta:
        lk = str(key).lower().replace("_", "-")
        if lk in {k.replace("_", "-") for k in _EXTENSION_KEYS} or key in _EXTENSION_KEYS:
            extensions.append(str(key))
            if str(key).lower() in {"context"} or lk == "context":
                vendorish = True
                notes.append("context_extension_detected")
            if "allowed-tools" in lk or "allowed_tools" in str(key):
                notes.append("allowed_tools_is_metadata_only_never_grants_permissions")

    if _BANG_COMMAND.search(body):
        extensions.append("!command")
        notes.append("bang_commands_returned_as_text_never_executed")

    if vendorish and extensions:
        return "vendor_specific", extensions, notes
    if extensions:
        return "portable_with_extensions", extensions, notes
    return "portable", extensions, notes


def discover_skills(jail: PathJail) -> list[SkillDoc]:
    skills: list[SkillDoc] = []
    for origin in SKILL_ROOTS:
        try:
            jail.open(origin, directory=True)
        except JailError:
            continue
        abs_origin = jail.root / origin
        try:
            names = sorted(os.listdir(abs_origin))
        except OSError:
            continue
        for name in names:
            skill_dir_rel = f"{origin}/{name}"
            skill_md_rel = f"{skill_dir_rel}/SKILL.md"
            try:
                jail.open(skill_dir_rel, directory=True)
            except JailError:
                continue
            try:
                raw = _read_bytes(jail, skill_md_rel)
            except JailError as exc:
                skills.append(
                    SkillDoc(
                        skill_id=skill_md_rel,
                        name=name,
                        description="",
                        origin=origin,
                        relative_dir=skill_dir_rel,
                        relative_skill_md=skill_md_rel,
                        path=str(jail.root / skill_md_rel),
                        sha256="",
                        compatibility="invalid",
                        warnings=[f"unreadable:{exc.code}"],
                    )
                )
                continue
            text = raw.decode("utf-8", errors="replace")
            meta, body = parse_frontmatter(text)
            compat, extensions, notes = classify_skill(meta, body)
            skill_name = str(meta.get("name") or name)
            description = str(meta.get("description") or "")
            supporting = _index_supporting(jail, skill_dir_rel)
            skills.append(
                SkillDoc(
                    skill_id=skill_md_rel,
                    name=skill_name,
                    description=description,
                    origin=origin,
                    relative_dir=skill_dir_rel,
                    relative_skill_md=skill_md_rel,
                    path=str(jail.root / skill_md_rel),
                    sha256=_sha(raw),
                    compatibility=compat,
                    extensions_detected=extensions,
                    compatibility_notes=notes,
                    body=body if body else text,
                    supporting_files=supporting,
                )
            )
    skills.sort(key=lambda s: s.skill_id)
    return skills


def _index_supporting(jail: PathJail, skill_dir_rel: str) -> list[dict[str, Any]]:
    """Index files under skill dir only (not SKILL.md). No reads of content."""
    out: list[dict[str, Any]] = []
    abs_dir = jail.root / skill_dir_rel
    for dirpath, dirnames, filenames in os.walk(abs_dir):
        dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__"}]
        for fname in filenames:
            if fname == "SKILL.md" and Path(dirpath) == abs_dir:
                continue
            full = Path(dirpath) / fname
            try:
                rel = str(full.relative_to(jail.root)).replace("\\", "/")
            except ValueError:
                continue
            # ensure still under skill dir via jail open
            try:
                fd = jail.open(rel)
                os.close(fd)
            except JailError as exc:
                out.append(
                    {
                        "relative": rel,
                        "included": False,
                        "warning": f"blocked:{exc.code}",
                    }
                )
                continue
            # reject if somehow escaped skill dir prefix
            if not rel.startswith(skill_dir_rel.rstrip("/") + "/"):
                out.append(
                    {
                        "relative": rel,
                        "included": False,
                        "warning": "outside_skill_dir",
                    }
                )
                continue
            st = full.stat()
            out.append(
                {
                    "relative": rel,
                    "included": True,
                    "size_bytes": st.st_size,
                    "note": "read_via_fs_read_if_needed_never_auto_executed",
                }
            )
    return out


def get_skill(jail: PathJail, skill_id: str) -> SkillDoc | None:
    """Resolve skill_id only if it matches a discovered skill under allowlisted roots."""
    if not skill_id or ".." in skill_id or skill_id.startswith("/"):
        return None
    skill_id = skill_id.replace("\\", "/")
    if skill_id.startswith("./"):
        skill_id = skill_id[2:]
    # must be <root>/<name>/SKILL.md
    parts = skill_id.split("/")
    if len(parts) != 4 or parts[3] != "SKILL.md":
        return None
    origin = f"{parts[0]}/{parts[1]}"
    if origin not in SKILL_ROOTS:
        return None
    for skill in discover_skills(jail):
        if skill.skill_id == skill_id:
            return skill
    return None
