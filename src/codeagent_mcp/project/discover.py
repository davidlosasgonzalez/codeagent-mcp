"""Discover instruction files under a PathJail root."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from codeagent_mcp.fs.openat2 import JailError, PathJail
from codeagent_mcp.project.frontmatter import parse_frontmatter
from codeagent_mcp.project.globs import path_matches_any

Activation = Literal["always", "path_scoped", "agent_requested", "manual"]
SourceType = Literal["agents_md", "claude_md", "cursor_rule"]


@dataclass(slots=True)
class InstructionDoc:
    instruction_id: str
    source_type: SourceType
    vendor: str
    path: str
    relative: str
    scope: str
    activation: Activation
    priority: int
    sha256: str
    description: str = ""
    globs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    body: str = ""
    truncated: bool = False
    references: list[str] = field(default_factory=list)

    def manifest(
        self, *, include_body: bool = False, max_bytes: int | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "instruction_id": self.instruction_id,
            "source_type": self.source_type,
            "vendor": self.vendor,
            "path": self.path,
            "relative": self.relative,
            "scope": self.scope,
            "activation": self.activation,
            "priority": self.priority,
            "sha256": self.sha256,
            "description": self.description,
            "globs": list(self.globs),
            "warnings": list(self.warnings),
            "references": list(self.references),
        }
        if include_body:
            body = self.body
            truncated = self.truncated
            if max_bytes is not None and len(body.encode("utf-8")) > max_bytes:
                encoded = body.encode("utf-8")[:max_bytes]
                body = encoded.decode("utf-8", errors="ignore")
                truncated = True
            payload["content"] = body
            payload["truncated"] = truncated
        return payload


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_text(
    jail: PathJail, rel: str, *, max_load: int = 2_000_000
) -> tuple[str, str, list[str]]:
    warnings: list[str] = []
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
        data = b"".join(chunks)
        digest = _sha(data)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            warnings.append("not_utf8")
            text = data.decode("utf-8", errors="replace")
            warnings.append("decoded_with_replacement")
        return text, digest, warnings
    finally:
        os.close(fd)


def _extract_at_refs(text: str) -> list[str]:
    # Claude-style @path references (simple)
    import re

    return sorted(set(re.findall(r"@([A-Za-z0-9_./-]+)", text)))


def discover_all(jail: PathJail) -> list[InstructionDoc]:
    docs: list[InstructionDoc] = []
    docs.extend(_discover_named(jail, "AGENTS.md", "agents_md", "portable", priority=10))
    docs.extend(_discover_named(jail, "CLAUDE.md", "claude_md", "claude", priority=20))
    docs.extend(_discover_cursor_rules(jail))
    return docs


def _discover_named(
    jail: PathJail,
    filename: str,
    source_type: SourceType,
    vendor: str,
    *,
    priority: int,
) -> list[InstructionDoc]:
    found: list[InstructionDoc] = []
    # walk via os.walk on jail.root after confirming root open — TOCTOU accepted after root fd
    for dirpath, dirnames, filenames in os.walk(jail.root):
        skip = {".git", ".venv", "node_modules", "__pycache__"}
        dirnames[:] = [d for d in dirnames if d not in skip]
        if filename not in filenames:
            continue
        full = Path(dirpath) / filename
        try:
            rel = str(full.relative_to(jail.root))
        except ValueError:
            continue
        try:
            text, digest, warnings = _read_text(jail, rel)
        except JailError as exc:
            found.append(
                InstructionDoc(
                    instruction_id=f"{source_type}:{rel}",
                    source_type=source_type,
                    vendor=vendor,
                    path=str(full),
                    relative=rel,
                    scope=str(Path(rel).parent).replace("\\", "/") or ".",
                    activation="always" if Path(rel).parent == Path(".") else "path_scoped",
                    priority=priority,
                    sha256="",
                    warnings=[f"unreadable:{exc.code}"],
                )
            )
            continue
        scope = str(Path(rel).parent).replace("\\", "/")
        if scope == ".":
            scope = "/"
            activation: Activation = "always"
        else:
            activation = "path_scoped"
        refs = _extract_at_refs(text)
        for ref in refs:
            try:
                jail.open(ref)
            except JailError:
                warnings.append(f"broken_reference:@{ref}")
        found.append(
            InstructionDoc(
                instruction_id=f"{source_type}:{rel}",
                source_type=source_type,
                vendor=vendor,
                path=str(full),
                relative=rel,
                scope=scope,
                activation=activation,
                priority=priority + (0 if scope == "/" else 5),
                sha256=digest,
                warnings=warnings,
                body=text,
                references=refs,
            )
        )
    return found


def _discover_cursor_rules(jail: PathJail) -> list[InstructionDoc]:
    rules_dir = ".cursor/rules"
    try:
        jail.open(rules_dir, directory=True)
    except JailError:
        return []
    docs: list[InstructionDoc] = []
    rules_path = jail.root / rules_dir
    for name in sorted(os.listdir(rules_path)):
        if not name.endswith(".mdc"):
            continue
        rel = f"{rules_dir}/{name}"
        try:
            text, digest, warnings = _read_text(jail, rel)
        except JailError as exc:
            docs.append(
                InstructionDoc(
                    instruction_id=f"cursor_rule:{rel}",
                    source_type="cursor_rule",
                    vendor="cursor",
                    path=str(jail.root / rel),
                    relative=rel,
                    scope="/",
                    activation="manual",
                    priority=30,
                    sha256="",
                    warnings=[f"unreadable:{exc.code}"],
                )
            )
            continue
        meta, body = parse_frontmatter(text)
        always = bool(meta.get("alwaysApply"))
        globs_raw = meta.get("globs")
        globs: list[str] = []
        if isinstance(globs_raw, list):
            globs = [str(g) for g in globs_raw]
        elif isinstance(globs_raw, str) and globs_raw:
            globs = [globs_raw]
        if always:
            activation: Activation = "always"
        elif globs:
            activation = "path_scoped"
        elif meta.get("description"):
            activation = "agent_requested"
        else:
            activation = "manual"
        docs.append(
            InstructionDoc(
                instruction_id=f"cursor_rule:{rel}",
                source_type="cursor_rule",
                vendor="cursor",
                path=str(jail.root / rel),
                relative=rel,
                scope="/",
                activation=activation,
                priority=30,
                sha256=digest,
                description=str(meta.get("description") or ""),
                globs=globs,
                warnings=warnings,
                body=body if body else text,
            )
        )
    return docs


def select_applicable(
    docs: list[InstructionDoc],
    *,
    paths: list[str],
    include_agent_requested: bool,
) -> list[InstructionDoc]:
    """Select docs for project_instructions.

    Empty paths: always-on root docs only (activation=always), plus path_scoped
    docs only when their scope is '/' and they are always — path_scoped with
    globs require a matching path.
    """
    selected: list[InstructionDoc] = []
    norm_paths = [p.replace("\\", "/").lstrip("./") for p in paths if p]

    for doc in docs:
        if doc.activation == "always":
            selected.append(doc)
            continue
        if doc.activation in {"agent_requested", "manual"}:
            if include_agent_requested:
                selected.append(doc)
            continue
        # path_scoped
        if not norm_paths:
            # nested CLAUDE/AGENTS at subdirectory not included without paths
            continue
        if doc.source_type == "cursor_rule":
            if any(path_matches_any(p, doc.globs) for p in norm_paths):
                selected.append(doc)
            continue
        # nested CLAUDE/AGENTS: include when any path is under scope or equals
        scope = doc.scope.strip("/")
        for p in norm_paths:
            if scope == "" or scope == "/":
                selected.append(doc)
                break
            if p == scope or p.startswith(scope + "/"):
                selected.append(doc)
                break
            # also: path is file under scope dir
            parent = str(Path(p).parent).replace("\\", "/")
            if parent == scope or parent.startswith(scope + "/"):
                selected.append(doc)
                break
    # stable order by priority then path
    selected.sort(key=lambda d: (d.priority, d.relative))
    return selected
