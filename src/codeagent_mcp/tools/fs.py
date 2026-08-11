"""MCP filesystem tools: read/search and lease-gated writes (text, binary, fileParams)."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from codeagent_mcp.adaptation.chatgpt_file_download import (
    download_openai_file_bytes,
    parse_openai_file_ref,
)
from codeagent_mcp.errors import tool_error
from codeagent_mcp.fs.service import (
    DEFAULT_MAX_LIST,
    DEFAULT_MAX_READ_BYTES,
    DEFAULT_MAX_SEARCH_MATCHES,
    FsService,
    project_or_error,
)
from codeagent_mcp.tools.annotations import DEST, DEST_OPEN, RO
from codeagent_mcp.tools.workspace import get_lease_manager


class OpenAIFileParam(BaseModel):
    """ChatGPT openai/fileParams object (adaptation). Strings only — no nulls in schema."""

    model_config = ConfigDict(extra="forbid")

    download_url: str
    file_id: str
    mime_type: str = Field(default="")
    file_name: str = Field(default="")


def _maybe_renew_lease(lease_id: str) -> dict[str, Any] | None:
    """Optional activity renew. Absent lease_id => None (reads allowed). Invalid => error dict."""
    if not lease_id or not lease_id.strip():
        return None
    result = get_lease_manager().require_active(lease_id=lease_id.strip())
    if result.get("ok") is not True:
        return result
    return None


def _resolve_project(project: str, lease_id: str) -> tuple[str, dict[str, Any] | None]:
    """If lease_id is set, bind to that lease project (ignore stale default=demo)."""
    if not lease_id or not str(lease_id).strip():
        return project, None
    result = get_lease_manager().require_active(lease_id=str(lease_id).strip())
    if result.get("ok") is not True:
        return project, result
    return str(result["project"]), None


def _require_writable_lease(
    *,
    tool_name: str,
    lease_id: str,
    path: str,
) -> FsService | dict[str, Any]:
    """Shared gates for mutating fs tools: lease → project → writable → FsService."""
    if not lease_id or not str(lease_id).strip():
        return tool_error(
            "LEASE_REQUIRED",
            f"lease_id is required for {tool_name}",
            retryable=False,
            next_action="Call workspace_acquire and pass lease_id",
        )
    lease_err = get_lease_manager().require_active(lease_id=str(lease_id).strip())
    if lease_err.get("ok") is not True:
        return lease_err
    project = str(lease_err["project"])
    if not path:
        return tool_error("INVALID_ARGUMENT", "path is required", retryable=False)
    from codeagent_mcp.workspace.projects import get_project

    cfg = get_project(project)
    if cfg is None:
        return tool_error(
            "INVALID_ARGUMENT",
            f"unknown project {project!r}",
            retryable=False,
        )
    if not cfg.writable:
        return tool_error(
            "WRITE_DISABLED",
            f"project {project!r} is not writable (enable the project write env gate)",
            retryable=False,
            next_action="Use a writable project id or set that project's writable_env=1",
        )
    svc = project_or_error(project)
    if isinstance(svc, dict):
        return svc
    return svc


def register_fs_tools(server: FastMCP) -> None:
    """Register fs_stat/list/read/search and lease-gated write tools on ``server``."""

    @server.tool(
        name="fs_stat",
        description=(
            "Stat a path under a registered project root (default demo). "
            "Read-only; lease_id optional (renews if provided). "
            "Does not follow escapes outside the root (openat2)."
        ),
        annotations=RO,
    )
    def fs_stat(
        path: str = "",
        project: str = "demo",
        lease_id: str = "",
    ) -> dict[str, Any]:
        project, err = _resolve_project(project, lease_id)
        if err is not None:
            return err
        svc = project_or_error(project)
        if isinstance(svc, dict):
            return svc
        return svc.stat(path)

    @server.tool(
        name="fs_list",
        description=(
            "List directory entries under a registered project root. "
            "Read-only; lease_id optional. Paths cannot escape the root."
        ),
        annotations=RO,
    )
    def fs_list(
        path: str = "",
        project: str = "demo",
        max_entries: int = DEFAULT_MAX_LIST,
        lease_id: str = "",
    ) -> dict[str, Any]:
        project, err = _resolve_project(project, lease_id)
        if err is not None:
            return err
        svc = project_or_error(project)
        if isinstance(svc, dict):
            return svc
        return svc.list_dir(path, max_entries=max_entries)

    @server.tool(
        name="fs_read",
        description=(
            "Read a UTF-8 text file under the project root with optional line range. "
            "Returns sha256 of the full file, content slice, and truncated flag. "
            "Rejects binaries (UNSUPPORTED_BINARY still includes sha256 for replace flows). "
            "Read-only; lease_id optional."
        ),
        annotations=RO,
    )
    def fs_read(
        path: str,
        project: str = "demo",
        start_line: int = 1,
        end_line: int = 0,
        max_bytes: int = DEFAULT_MAX_READ_BYTES,
        lease_id: str = "",
    ) -> dict[str, Any]:
        project, err = _resolve_project(project, lease_id)
        if err is not None:
            return err
        if not path:
            return tool_error("INVALID_ARGUMENT", "path is required", retryable=False)
        svc = project_or_error(project)
        if isinstance(svc, dict):
            return svc
        end = None if end_line == 0 else end_line
        return svc.read(path, start_line=start_line, end_line=end, max_bytes=max_bytes)

    @server.tool(
        name="fs_search",
        description=(
            "Search file contents under the project root using ripgrep (no symlink follow). "
            "Supports literal or regex query; max_matches capped. Read-only; lease_id optional."
        ),
        annotations=RO,
    )
    def fs_search(
        query: str,
        path: str = "",
        project: str = "demo",
        literal: bool = False,
        max_matches: int = DEFAULT_MAX_SEARCH_MATCHES,
        glob: str = "",
        lease_id: str = "",
    ) -> dict[str, Any]:
        project, err = _resolve_project(project, lease_id)
        if err is not None:
            return err
        svc = project_or_error(project)
        if isinstance(svc, dict):
            return svc
        return svc.search(
            query,
            path=path,
            literal=literal,
            max_matches=max_matches,
            glob=glob or "",
        )

    @server.tool(
        name="fs_apply_patch",
        description=(
            "Apply a structured text patch under the project root. "
            "Requires lease_id. For existing files, expected_sha256 from fs_read is mandatory "
            "(CONFLICT if the file changed). Pass edits=[{old_string,new_string},...] "
            "or new_content for a full replace. create=true allows new files "
            "(empty expected_sha256). Atomic temp+rename in the same directory. "
            "Write to production checkouts requires the matching writable_env "
            "gate declared in projects.yaml. Use stable environment-specific "
            "project identifiers."
        ),
        annotations=DEST,
    )
    def fs_apply_patch(
        path: str,
        lease_id: str,
        expected_sha256: str = "",
        edits: list[dict[str, str]] | None = None,
        new_content: str | None = None,
        create: bool = False,
        project: str = "demo",
    ) -> dict[str, Any]:
        _ = project  # client hint only; lease project wins
        gated = _require_writable_lease(
            tool_name="fs_apply_patch",
            lease_id=lease_id,
            path=path,
        )
        if isinstance(gated, dict):
            return gated
        return gated.apply_patch(
            path,
            expected_sha256=expected_sha256,
            edits=edits,
            new_content=new_content,
            create=create,
        )

    @server.tool(
        name="fs_write_binary",
        description=(
            "Write a binary file under the project root from plain Base64 "
            "(not a data URL; no remote fetch). Requires lease_id. "
            "Line breaks and the URL-safe alphabet are accepted; padding is optional. "
            "Decoded size capped at 2_000_000 bytes (coupled with Caddy 4MB body limit). "
            "For existing files, expected_sha256 is mandatory (use sha256 from "
            "fs_read UNSUPPORTED_BINARY); CONFLICT if the file changed. "
            "create=true allows new files (empty expected_sha256). "
            "Atomic temp+fsync+replace. Same write gates as fs_apply_patch."
        ),
        annotations=DEST,
    )
    def fs_write_binary(
        path: str,
        lease_id: str,
        content_base64: str,
        expected_sha256: str = "",
        create: bool = False,
        project: str = "demo",
    ) -> dict[str, Any]:
        _ = project  # client hint only; lease project wins
        gated = _require_writable_lease(
            tool_name="fs_write_binary",
            lease_id=lease_id,
            path=path,
        )
        if isinstance(gated, dict):
            return gated
        return gated.write_binary(
            path,
            content_base64=content_base64,
            expected_sha256=expected_sha256,
            create=create,
        )

    @server.tool(
        name="fs_write_file",
        description=(
            "Write a binary file from a ChatGPT host file reference "
            "(openai/fileParams: download_url + file_id). Requires lease_id. "
            "Preferred way to move an uploaded or ImageGen-generated file into a "
            "project — pass the file itself, never Base64 of it. "
            "Downloads over HTTPS from an allowlisted OpenAI host only, including "
            "the code-interpreter sandbox storage that backs /mnt/data "
            "(no redirects; size capped at 2_000_000 bytes during stream). "
            "Same SHA/create/write gates as fs_write_binary. "
            "Portable clients should use fs_write_binary (Base64) instead. "
            "path is authoritative — file_name/mime_type are metadata only."
        ),
        annotations=DEST_OPEN,
        meta={"openai/fileParams": ["file"]},
    )
    def fs_write_file(
        path: str,
        lease_id: str,
        file: OpenAIFileParam,
        expected_sha256: str = "",
        create: bool = False,
        project: str = "demo",
    ) -> dict[str, Any]:
        _ = project  # client hint only; lease project wins
        gated = _require_writable_lease(
            tool_name="fs_write_file",
            lease_id=lease_id,
            path=path,
        )
        if isinstance(gated, dict):
            return gated
        parsed = parse_openai_file_ref(file)
        if isinstance(parsed, dict):
            return parsed
        payload = download_openai_file_bytes(parsed)
        if isinstance(payload, dict):
            return payload
        # Re-check lease after potentially long download (TTL / concurrent release).
        gated = _require_writable_lease(
            tool_name="fs_write_file",
            lease_id=lease_id,
            path=path,
        )
        if isinstance(gated, dict):
            return gated
        return gated.write_bytes(
            path,
            data=payload,
            expected_sha256=expected_sha256,
            create=create,
        )
