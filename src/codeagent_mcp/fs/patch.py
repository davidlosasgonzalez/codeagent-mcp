"""Atomic structured patch apply under PathJail."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat as statmod
from pathlib import Path
from typing import Any

from codeagent_mcp.errors import tool_error, tool_ok
from codeagent_mcp.fs.binary import is_binary
from codeagent_mcp.fs.openat2 import JailError, PathJail


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _apply_edits(text: str, edits: list[dict[str, str]]) -> tuple[str, dict[str, int]]:
    """Apply sequential unique old→new replacements. Raises ValueError on ambiguity/miss."""
    lines_before = text.count("\n") + (0 if text.endswith("\n") or text == "" else 1)
    if text == "":
        lines_before = 0
    cur = text
    for i, edit in enumerate(edits):
        old = edit.get("old_string")
        new = edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValueError(f"edits[{i}] must have string old_string and new_string")
        if old == "":
            raise ValueError(f"edits[{i}].old_string must be non-empty")
        count = cur.count(old)
        if count == 0:
            raise ValueError(f"edits[{i}]: old_string not found")
        if count > 1:
            raise ValueError(f"edits[{i}]: old_string matches {count} times; must be unique")
        cur = cur.replace(old, new, 1)
    lines_after = cur.count("\n") + (0 if cur.endswith("\n") or cur == "" else 1)
    if cur == "":
        lines_after = 0
    return cur, {
        "lines_before": lines_before,
        "lines_after": lines_after,
        "lines_delta": lines_after - lines_before,
        "edits_applied": len(edits),
    }


def apply_structured_patch(
    *,
    root: str,
    path: str,
    expected_sha256: str,
    edits: list[dict[str, str]] | None = None,
    new_content: str | None = None,
    create: bool = False,
) -> dict[str, Any]:
    """Apply structured patch. expected_sha256 required when file exists."""
    if (edits is None or len(edits) == 0) and new_content is None:
        return tool_error(
            "INVALID_ARGUMENT",
            "provide non-empty edits and/or new_content",
            retryable=False,
        )
    if edits and new_content is not None:
        return tool_error(
            "INVALID_ARGUMENT",
            "pass either edits or new_content, not both",
            retryable=False,
        )

    try:
        with PathJail(root) as jail:
            rel = jail.to_relative(path)
            if rel in {"", "."}:
                return tool_error(
                    "INVALID_ARGUMENT",
                    "path must be a file, not the project root",
                    retryable=False,
                )
            parent_rel = str(Path(rel).parent)
            if parent_rel == ".":
                parent_rel_path = ""
            else:
                parent_rel_path = parent_rel
            # Confinement check on parent dir
            parent_fd = jail.open(parent_rel_path, directory=True)
            os.close(parent_fd)

            final = Path(jail.display_path(path))
            existing_data: bytes | None = None
            mode = 0o644
            try:
                fd = jail.open(path)
                try:
                    st = os.fstat(fd)
                    if not statmod.S_ISREG(st.st_mode):
                        return tool_error(
                            "INVALID_ARGUMENT",
                            "fs_apply_patch requires a regular file",
                            retryable=False,
                        )
                    mode = statmod.S_IMODE(st.st_mode)
                    chunks: list[bytes] = []
                    while True:
                        block = os.read(fd, 1024 * 1024)
                        if not block:
                            break
                        chunks.append(block)
                    existing_data = b"".join(chunks)
                finally:
                    os.close(fd)
            except JailError as exc:
                if exc.code != "NOT_FOUND":
                    return tool_error(exc.code, exc.message, retryable=False)
                if not create:
                    return tool_error(
                        "NOT_FOUND",
                        f"path not found: {path!r}; pass create=true for new files",
                        retryable=False,
                        next_action="Call fs_stat/fs_read or set create=true",
                    )
                if expected_sha256:
                    return tool_error(
                        "INVALID_ARGUMENT",
                        "expected_sha256 must be empty when create=true for a new file",
                        retryable=False,
                    )
                existing_data = None

            if existing_data is not None:
                if not expected_sha256:
                    return tool_error(
                        "INVALID_ARGUMENT",
                        "expected_sha256 is required for existing files",
                        retryable=False,
                        next_action="fs_read the file and pass its sha256",
                    )
                current = _sha256_bytes(existing_data)
                if current != expected_sha256:
                    return tool_error(
                        "CONFLICT",
                        "file changed since expected_sha256",
                        retryable=True,
                        next_action="Call fs_read again and retry with the new sha256",
                        current_sha256=current,
                        expected_sha256=expected_sha256,
                    )
                if is_binary(existing_data):
                    return tool_error(
                        "UNSUPPORTED_BINARY",
                        "refusing to patch binary file as text",
                        retryable=False,
                    )
                try:
                    text = existing_data.decode("utf-8")
                except UnicodeDecodeError:
                    return tool_error(
                        "UNSUPPORTED_BINARY",
                        "file is not valid UTF-8 text",
                        retryable=False,
                    )
            else:
                text = ""

            summary: dict[str, int]
            try:
                if new_content is not None:
                    if not isinstance(new_content, str):
                        return tool_error(
                            "INVALID_ARGUMENT",
                            "new_content must be a string",
                            retryable=False,
                        )
                    before = text.count("\n") + (0 if text.endswith("\n") or text == "" else 1)
                    if text == "":
                        before = 0
                    after = new_content.count("\n") + (
                        0 if new_content.endswith("\n") or new_content == "" else 1
                    )
                    if new_content == "":
                        after = 0
                    text = new_content
                    summary = {
                        "lines_before": before,
                        "lines_after": after,
                        "lines_delta": after - before,
                        "edits_applied": 0,
                        "full_replace": 1,
                    }
                else:
                    assert edits is not None
                    text, summary = _apply_edits(text, edits)
            except ValueError as exc:
                return tool_error("INVALID_ARGUMENT", str(exc), retryable=False)

            new_bytes = text.encode("utf-8")
            new_hash = _sha256_bytes(new_bytes)
            tmp = final.parent / f".codeagent-tmp-{os.getpid()}-{secrets.token_hex(4)}"
            try:
                with open(tmp, "wb") as handle:
                    handle.write(new_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp, mode)
                os.replace(tmp, final)
                dir_fd = os.open(final.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except PermissionError:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                return tool_error(
                    "PERMISSION_DENIED",
                    f"cannot write {path!r} (checkout may be read-only until C8)",
                    retryable=False,
                )
            except OSError as exc:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                if getattr(exc, "errno", None) in {13, 30}:  # EACCES, EROFS
                    return tool_error(
                        "PERMISSION_DENIED",
                        f"cannot write {path!r}: {exc}",
                        retryable=False,
                    )
                return tool_error(
                    "INTERNAL_ERROR",
                    f"write failed: {exc}",
                    retryable=True,
                )

            return tool_ok(
                project="",  # filled by FsService
                path=str(final),
                relative=rel,
                sha256=new_hash,
                created=existing_data is None,
                summary=summary,
            )
    except JailError as exc:
        return tool_error(exc.code, exc.message, retryable=False)
