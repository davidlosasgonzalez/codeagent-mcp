"""Durable pane spool: pipe-pane target, cursor reads, ANSI sanitize."""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SPOOL_ROOT = "/var/lib/codeagent-mcp/spool"
DEFAULT_MAX_READ_BYTES = 100_000
DEFAULT_MAX_SPOOL_BYTES = 2_000_000
HARD_MAX_READ_BYTES = 300_000

_ANSI_RE = re.compile(
    rb"(?:"
    rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    rb"|\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI
    rb"|\x1b[PX^_][^\x1b]*\x1b\\"  # DCS/PM/APC
    rb"|\x1b."  # other short ESC
    rb")"
)
_C0_RE = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class CursorExpired(Exception):
    def __init__(self, message: str, *, retained_start: int, generation: str) -> None:
        super().__init__(message)
        self.retained_start = retained_start
        self.generation = generation


@dataclass(frozen=True)
class Cursor:
    generation: str
    offset: int

    def encode(self) -> str:
        return f"v1:{self.generation}:{self.offset}"

    @staticmethod
    def decode(raw: str | None) -> Cursor | None:
        if raw is None or raw == "" or raw == "0":
            return None
        parts = str(raw).split(":")
        if len(parts) != 3 or parts[0] != "v1":
            raise ValueError("cursor must look like v1:<generation>:<offset>")
        gen, off_s = parts[1], parts[2]
        if not gen or not re.fullmatch(r"[0-9a-f]{8,64}", gen):
            raise ValueError("invalid cursor generation")
        try:
            off = int(off_s)
        except ValueError as exc:
            raise ValueError("invalid cursor offset") from exc
        if off < 0:
            raise ValueError("cursor offset must be >= 0")
        return Cursor(generation=gen, offset=off)


def spool_root() -> Path:
    return Path(os.environ.get("CODEAGENT_SPOOL_ROOT", DEFAULT_SPOOL_ROOT))


def new_generation() -> str:
    return uuid.uuid4().hex


def spool_path_for(generation: str) -> Path:
    root = spool_root()
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    return root / f"{generation}.log"


def ensure_spool_file(path: Path) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not path.exists():
        path.touch(mode=0o600)


def physical_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def sanitize_for_client(raw: bytes) -> tuple[str, bool]:
    """Option A: project sanitized text; spool stays raw."""
    looked_binary = b"\x00" in raw
    cleaned = _ANSI_RE.sub(b"", raw)
    cleaned = _C0_RE.sub(b"", cleaned)
    text = cleaned.decode("utf-8", errors="replace")
    return text, looked_binary


def read_spool(
    *,
    path: Path,
    generation: str,
    byte_base: int,
    cursor: Cursor | None,
    max_bytes: int,
) -> dict:
    if max_bytes < 1:
        raise ValueError("max_bytes must be >= 1")
    max_bytes = min(int(max_bytes), HARD_MAX_READ_BYTES)

    size = physical_size(path)
    logical_end = byte_base + size

    if cursor is None:
        start = byte_base
    else:
        if cursor.generation != generation:
            raise CursorExpired(
                "cursor generation no longer retained",
                retained_start=byte_base,
                generation=generation,
            )
        if cursor.offset < byte_base:
            raise CursorExpired(
                "cursor offset was rotated away",
                retained_start=byte_base,
                generation=generation,
            )
        start = cursor.offset

    if start > logical_end:
        start = logical_end

    physical = start - byte_base
    to_read = min(max_bytes, max(0, size - physical))
    raw = b""
    if to_read:
        with open(path, "rb") as fh:
            fh.seek(physical)
            raw = fh.read(to_read)

    next_off = start + len(raw)
    has_more = next_off < logical_end
    text, binary = sanitize_for_client(raw)
    return {
        "text": text,
        "raw_byte_len": len(raw),
        "next_cursor": Cursor(generation=generation, offset=next_off).encode(),
        "has_more": has_more,
        "truncated": False,
        "binary_suspected": binary,
        "generation": generation,
        "byte_base": byte_base,
    }


def rotate_file(
    *, path: Path, byte_base: int, max_spool_bytes: int = DEFAULT_MAX_SPOOL_BYTES
) -> tuple[int, bool]:
    """Replace oversized spool file. Caller must stop/restart pipe-pane around this.

    Returns (new_byte_base, rotated).
    """
    size = physical_size(path)
    if size < max_spool_bytes:
        return byte_base, False
    archive = path.with_name(path.name + f".{byte_base + size}.bak")
    try:
        if path.exists():
            os.replace(path, archive)
    except OSError:
        path.unlink(missing_ok=True)
    ensure_spool_file(path)
    try:
        archive.unlink(missing_ok=True)
    except OSError:
        pass
    return byte_base + size, True


def delete_spool(path: str | Path | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
