"""Atomic binary write under PathJail (bytes or Base64)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import secrets
import stat as statmod
from pathlib import Path
from typing import Any

from codeagent_mcp.errors import tool_error, tool_ok
from codeagent_mcp.fs.openat2 import JailError, PathJail

# Decoded-byte / stream cap. Coupled with Caddy request_body max_size 4MB (decimal):
# Base64 of 2_000_000 ≈ 2.67MB; ~33% headroom under 4_000_000. Raise only with
# proxy limit + threat tests together. Aligned with HARD_MAX_READ_BYTES.
MAX_BINARY_WRITE_BYTES = 2_000_000


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_BASE64_WHITESPACE = str.maketrans("", "", " \t\r\n\x0b\x0c")


def _normalize_base64(raw: str) -> str:
    """Make agent-pasted Base64 decodable: drop whitespace, accept base64url, pad.

    Encoding shape only — the size and SHA gates below are untouched. Agents wrap
    long Base64 across lines and some emit the URL-safe alphabet; rejecting that
    turned a one-shot binary write into manual retries.
    """
    text = raw.translate(_BASE64_WHITESPACE)
    text = text.replace("-", "+").replace("_", "/")
    remainder = len(text) % 4
    if remainder:
        text += "=" * (4 - remainder)
    return text


def _decode_content_base64(content_base64: str) -> dict[str, Any] | bytes:
    """Decode Base64 with size gates. Never returns the input string."""
    if not isinstance(content_base64, str):
        return tool_error(
            "INVALID_ARGUMENT",
            "content_base64 must be a string",
            retryable=False,
        )
    if content_base64.lstrip().startswith("data:"):
        return tool_error(
            "INVALID_ARGUMENT",
            "content_base64 must be plain Base64, not a data URL",
            retryable=False,
            next_action="Send only the part after the comma in the data URL",
        )
    normalized = _normalize_base64(content_base64)
    # Approximate decoded size before allocating the full decode buffer.
    approx = (len(normalized) * 3) // 4
    if approx > MAX_BINARY_WRITE_BYTES:
        return tool_error(
            "INVALID_ARGUMENT",
            f"decoded payload would exceed {MAX_BINARY_WRITE_BYTES} bytes (approx {approx})",
            retryable=False,
        )
    try:
        data = base64.b64decode(normalized, validate=True)
    except (binascii.Error, ValueError) as exc:
        return tool_error(
            "INVALID_ARGUMENT",
            f"invalid Base64: {exc}",
            retryable=False,
            next_action=(
                "Send the whole Base64 payload in one argument, unmodified and "
                "untruncated; for files over ~1MB use fs_write_file instead"
            ),
        )
    if len(data) > MAX_BINARY_WRITE_BYTES:
        return tool_error(
            "INVALID_ARGUMENT",
            f"decoded payload exceeds {MAX_BINARY_WRITE_BYTES} bytes (got {len(data)})",
            retryable=False,
        )
    return data


def write_bytes(
    *,
    root: str,
    path: str,
    data: bytes,
    expected_sha256: str = "",
    create: bool = False,
) -> dict[str, Any]:
    """Write raw bytes under PathJail. expected_sha256 required when file exists."""
    if not isinstance(data, (bytes, bytearray)):
        return tool_error(
            "INVALID_ARGUMENT",
            "data must be bytes",
            retryable=False,
        )
    new_bytes = bytes(data)
    if len(new_bytes) > MAX_BINARY_WRITE_BYTES:
        return tool_error(
            "INVALID_ARGUMENT",
            f"payload exceeds {MAX_BINARY_WRITE_BYTES} bytes (got {len(new_bytes)})",
            retryable=False,
        )
    new_hash = _sha256_bytes(new_bytes)

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
            parent_rel_path = "" if parent_rel == "." else parent_rel
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
                            "binary write requires a regular file",
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
                        next_action="Call fs_stat or set create=true",
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
                        next_action=(
                            "Call fs_read (UNSUPPORTED_BINARY includes sha256) or "
                            "fs_stat path metadata, then retry"
                        ),
                    )
                current = _sha256_bytes(existing_data)
                if current != expected_sha256:
                    return tool_error(
                        "CONFLICT",
                        "file changed since expected_sha256",
                        retryable=True,
                        next_action=("Call fs_read again (binary error includes sha256) and retry"),
                        current_sha256=current,
                        expected_sha256=expected_sha256,
                    )

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
                    f"cannot write {path!r}",
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
                size_bytes=len(new_bytes),
                created=existing_data is None,
            )
    except JailError as exc:
        return tool_error(exc.code, exc.message, retryable=False)


def write_binary(
    *,
    root: str,
    path: str,
    content_base64: str,
    expected_sha256: str = "",
    create: bool = False,
) -> dict[str, Any]:
    """Write binary bytes from Base64. expected_sha256 required when file exists."""
    decoded = _decode_content_base64(content_base64)
    if isinstance(decoded, dict):
        return decoded
    return write_bytes(
        root=root,
        path=path,
        data=decoded,
        expected_sha256=expected_sha256,
        create=create,
    )
