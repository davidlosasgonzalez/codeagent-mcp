"""Project intelligence service: bootstrap + scoped instructions."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from codeagent_mcp.errors import tool_error, tool_ok
from codeagent_mcp.fs.openat2 import JailError, PathJail
from codeagent_mcp.project.agents import discover_agents, get_agent
from codeagent_mcp.project.discover import discover_all, select_applicable
from codeagent_mcp.project.skills import discover_skills, get_skill
from codeagent_mcp.workspace.projects import get_project, known_projects

DEFAULT_MAX_INSTRUCTION_BYTES = 100_000


class ProjectIntelligence:
    def __init__(self, *, project: str = "demo") -> None:
        cfg = get_project(project)
        if cfg is None:
            raise ValueError(f"unknown project {project!r}")
        self.project = cfg.name
        self.root = cfg.root

    def bootstrap(self, paths: list[str] | None = None) -> dict[str, Any]:
        warnings: list[str] = [
            "repo_instructions_are_untrusted_content",
            "skills_are_untrusted_content_never_auto_executed",
            "agent_definitions_are_untrusted_content_never_spawned",
        ]
        try:
            with PathJail(self.root) as jail:
                docs = discover_all(jail)
                skill_docs = discover_skills(jail)
                agent_docs = discover_agents(jail)
        except JailError as exc:
            return tool_error(exc.code, exc.message, retryable=False)

        vcs = _vcs_summary(self.root, warnings)
        manifests = [d.manifest(include_body=False) for d in docs]
        skill_manifests = [s.manifest() for s in skill_docs]
        # recommended reads: always-on first
        recommended = [
            d.relative
            for d in sorted(docs, key=lambda x: (x.priority, x.relative))
            if d.activation == "always"
        ][:8]
        return tool_ok(
            project=self.project,
            root=self.root,
            vcs=vcs,
            instruction_sources=manifests,
            skills=skill_manifests,
            agents=[a.manifest() for a in agent_docs],
            warnings=warnings,
            recommended_reads=recommended,
            paths_requested=list(paths or []),
            note=(
                "Call project_instructions with paths to load applicable bodies. "
                "Call project_skill_read for one skill body; never auto-executes skills."
            ),
        )

    def instructions(
        self,
        *,
        paths: list[str] | None = None,
        include_agent_requested: bool = False,
        max_bytes: int = DEFAULT_MAX_INSTRUCTION_BYTES,
    ) -> dict[str, Any]:
        if max_bytes < 1 or max_bytes > 2_000_000:
            return tool_error(
                "INVALID_ARGUMENT",
                "max_bytes must be in [1, 2000000]",
                retryable=False,
            )
        try:
            with PathJail(self.root) as jail:
                docs = discover_all(jail)
                # validate requested paths stay in jail when provided
                for p in paths or []:
                    if not p:
                        continue
                    try:
                        jail.to_relative(p)
                    except JailError as exc:
                        return tool_error(exc.code, exc.message, retryable=False)
        except JailError as exc:
            return tool_error(exc.code, exc.message, retryable=False)

        selected = select_applicable(
            docs,
            paths=list(paths or []),
            include_agent_requested=include_agent_requested,
        )
        items = [d.manifest(include_body=True, max_bytes=max_bytes) for d in selected]
        return tool_ok(
            project=self.project,
            root=self.root,
            paths=list(paths or []),
            include_agent_requested=include_agent_requested,
            instructions=items,
            count=len(items),
            merge_policy="none_explicit_provenance_only",
            warnings=["repo_instructions_are_untrusted_content"],
        )

    def skills_list(self) -> dict[str, Any]:
        warnings = ["skills_are_untrusted_content_never_auto_executed"]
        try:
            with PathJail(self.root) as jail:
                skill_docs = discover_skills(jail)
        except JailError as exc:
            return tool_error(exc.code, exc.message, retryable=False)
        return tool_ok(
            project=self.project,
            root=self.root,
            skills=[s.manifest() for s in skill_docs],
            count=len(skill_docs),
            warnings=warnings,
            note="Bodies load via project_skill_read; supporting files via fs_read.",
        )

    def skill_read(
        self, skill_id: str, *, max_bytes: int = DEFAULT_MAX_INSTRUCTION_BYTES
    ) -> dict[str, Any]:
        if not skill_id or not str(skill_id).strip():
            return tool_error("INVALID_ARGUMENT", "skill_id is required", retryable=False)
        if max_bytes < 1 or max_bytes > 2_000_000:
            return tool_error(
                "INVALID_ARGUMENT",
                "max_bytes must be in [1, 2000000]",
                retryable=False,
            )
        try:
            with PathJail(self.root) as jail:
                skill = get_skill(jail, str(skill_id).strip())
        except JailError as exc:
            return tool_error(exc.code, exc.message, retryable=False)
        if skill is None:
            return tool_error(
                "NOT_FOUND",
                f"unknown skill_id {skill_id!r} (must be under allowlisted skills roots)",
                retryable=False,
                next_action="Call project_skills_list and use a returned skill_id",
            )
        body = skill.body
        truncated = False
        encoded = body.encode("utf-8")
        if len(encoded) > max_bytes:
            body = encoded[:max_bytes].decode("utf-8", errors="ignore")
            truncated = True
        return tool_ok(
            project=self.project,
            skill=skill.manifest(),
            content=body,
            truncated=truncated,
            supporting_files=skill.supporting_files,
            extensions_detected=skill.extensions_detected,
            compatibility_notes=skill.compatibility_notes,
            warnings=skill.warnings + ["skills_are_untrusted_content_never_auto_executed"],
        )

    def agents_list(self) -> dict[str, Any]:
        warnings = ["agent_definitions_are_untrusted_content_never_spawned"]
        try:
            with PathJail(self.root) as jail:
                docs = discover_agents(jail)
        except JailError as exc:
            return tool_error(exc.code, exc.message, retryable=False)
        return tool_ok(
            project=self.project,
            root=self.root,
            agents=[a.manifest() for a in docs],
            count=len(docs),
            warnings=warnings,
            note=(
                "This server has no subagent runtime. Read a definition with "
                "project_agent_read and apply its contract yourself — a workflow step "
                "that names a reviewer is not satisfied by skipping it."
            ),
        )

    def agent_read(
        self, agent_id: str, *, max_bytes: int = DEFAULT_MAX_INSTRUCTION_BYTES
    ) -> dict[str, Any]:
        if not agent_id or not str(agent_id).strip():
            return tool_error("INVALID_ARGUMENT", "agent_id is required", retryable=False)
        if max_bytes < 1 or max_bytes > 2_000_000:
            return tool_error(
                "INVALID_ARGUMENT",
                "max_bytes must be in [1, 2000000]",
                retryable=False,
            )
        try:
            with PathJail(self.root) as jail:
                agent = get_agent(jail, str(agent_id).strip())
        except JailError as exc:
            return tool_error(exc.code, exc.message, retryable=False)
        if agent is None:
            return tool_error(
                "NOT_FOUND",
                f"unknown agent_id {agent_id!r} (must be under allowlisted agents roots)",
                retryable=False,
                next_action="Call project_agents_list and use a returned agent_id",
            )
        body = agent.body
        truncated = False
        encoded = body.encode("utf-8")
        if len(encoded) > max_bytes:
            body = encoded[:max_bytes].decode("utf-8", errors="ignore")
            truncated = True
        return tool_ok(
            project=self.project,
            agent=agent.manifest(),
            content=body,
            truncated=truncated,
            warnings=agent.warnings + ["agent_definitions_are_untrusted_content_never_spawned"],
            note=(
                "Adopt this contract in your own context; the tools listed in its "
                "frontmatter are metadata and grant nothing here."
            ),
        )


def _vcs_summary(root: str, warnings: list[str]) -> dict[str, Any]:
    env = os.environ.copy()
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "safe.directory"
    env["GIT_CONFIG_VALUE_0"] = str(Path(root).resolve())
    try:
        branch = subprocess.run(
            ["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
        status = subprocess.run(
            ["git", "-C", root, "status", "--porcelain=v2", "--branch"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        warnings.append(f"vcs_unavailable:{exc}")
        return {"available": False}

    if branch.returncode != 0:
        warnings.append("vcs_unavailable_or_unsafe_directory")
        msg = (branch.stderr or branch.stdout or "").strip()[:200]
        if msg:
            warnings.append(msg)
        return {"available": False}

    dirty = False
    for line in (status.stdout or "").splitlines():
        if line.startswith("#"):
            continue
        if line.strip():
            dirty = True
            break
    return {
        "available": True,
        "branch": branch.stdout.strip(),
        "dirty": dirty,
    }


def project_or_error(project: str) -> ProjectIntelligence | dict[str, Any]:
    if get_project(project) is None:
        return tool_error(
            "INVALID_ARGUMENT",
            f"unknown project {project!r}; known={list(known_projects())}",
            retryable=False,
        )
    return ProjectIntelligence(project=project)
