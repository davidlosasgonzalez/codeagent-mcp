"""Subagent definition discovery and read. Never spawns anything.

A repo's workflow skills routinely delegate a step to a named subagent — "hand
the diff to the reviewer before committing". CodeAgent has no subagent runtime,
so on this surface that step silently did not happen: the caller could not even
see what the reviewer was supposed to check.

Exposing the definitions closes the honest half of that gap. The caller reads
the reviewer's own contract and applies it itself, in its own context, instead
of skipping the step or inventing a review standard. It is a role to adopt, not
a process to launch — nothing here executes.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any

from codeagent_mcp.fs.openat2 import JailError, PathJail
from codeagent_mcp.project.frontmatter import parse_frontmatter

AGENT_ROOTS = (
    ".claude/agents",
    ".cursor/agents",
    ".agents/agents",
    ".codex/agents",
)

MAX_AGENT_BYTES = 2_000_000


@dataclass(slots=True)
class AgentDoc:
    agent_id: str
    name: str
    description: str
    origin: str
    relative: str
    path: str
    sha256: str
    tools: str = ""
    model: str = ""
    warnings: list[str] = field(default_factory=list)
    body: str = ""

    def manifest(self, *, description_max: int = 400) -> dict[str, Any]:
        desc = self.description
        truncated = False
        if len(desc) > description_max:
            desc = desc[:description_max]
            truncated = True
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "description": desc,
            "description_truncated": truncated,
            "origin": self.origin,
            "relative": self.relative,
            "tools": self.tools,
            "model": self.model,
            "warnings": list(self.warnings),
            "sha256": self.sha256,
        }


def _read_bytes(jail: PathJail, rel: str, *, max_load: int = MAX_AGENT_BYTES) -> bytes:
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


def _meta_str(meta: dict[str, Any], key: str) -> str:
    value = meta.get(key)
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def discover_agents(jail: PathJail) -> list[AgentDoc]:
    """Index every ``*.md`` directly under an allowlisted agents root."""
    agents: list[AgentDoc] = []
    for origin in AGENT_ROOTS:
        try:
            jail.open(origin, directory=True)
        except JailError:
            continue
        try:
            names = sorted(os.listdir(jail.root / origin))
        except OSError:
            continue
        for fname in names:
            if not fname.endswith(".md"):
                continue
            rel = f"{origin}/{fname}"
            stem = fname[: -len(".md")]
            try:
                raw = _read_bytes(jail, rel)
            except JailError as exc:
                agents.append(
                    AgentDoc(
                        agent_id=rel,
                        name=stem,
                        description="",
                        origin=origin,
                        relative=rel,
                        path=str(jail.root / rel),
                        sha256="",
                        warnings=[f"unreadable:{exc.code}"],
                    )
                )
                continue
            text = raw.decode("utf-8", errors="replace")
            meta, body = parse_frontmatter(text)
            warnings: list[str] = []
            if not _meta_str(meta, "description"):
                warnings.append("missing_description")
            agents.append(
                AgentDoc(
                    agent_id=rel,
                    name=_meta_str(meta, "name") or stem,
                    description=_meta_str(meta, "description"),
                    origin=origin,
                    relative=rel,
                    path=str(jail.root / rel),
                    sha256=hashlib.sha256(raw).hexdigest(),
                    tools=_meta_str(meta, "tools"),
                    model=_meta_str(meta, "model"),
                    warnings=warnings,
                    body=body if body else text,
                )
            )
    agents.sort(key=lambda a: a.agent_id)
    return agents


def get_agent(jail: PathJail, agent_id: str) -> AgentDoc | None:
    """Resolve ``agent_id`` only if it names a discovered file under an allowlisted root."""
    if not agent_id or ".." in agent_id or agent_id.startswith("/"):
        return None
    agent_id = agent_id.replace("\\", "/")
    if agent_id.startswith("./"):
        agent_id = agent_id[2:]
    parts = agent_id.split("/")
    if len(parts) != 3 or not parts[2].endswith(".md"):
        return None
    if f"{parts[0]}/{parts[1]}" not in AGENT_ROOTS:
        return None
    for agent in discover_agents(jail):
        if agent.agent_id == agent_id:
            return agent
    return None
