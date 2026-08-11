"""Linux openat2 path confinement (RESOLVE_BENEATH + NO_MAGICLINKS)."""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
from dataclasses import dataclass
from pathlib import Path

# linux/openat2.h
RESOLVE_NO_XDEV = 0x01
RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08
RESOLVE_IN_ROOT = 0x10

SYS_openat2 = 437  # x86_64 / aarch64 Linux

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.syscall.restype = ctypes.c_long


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


@dataclass(frozen=True, slots=True)
class JailError(Exception):
    code: str
    message: str


class PathJail:
    """Open paths only beneath an authorized root using openat2."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise JailError("NOT_FOUND", f"project root is not a directory: {self.root}")
        self._root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)

    def close(self) -> None:
        try:
            os.close(self._root_fd)
        except OSError:
            pass

    def __enter__(self) -> PathJail:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def to_relative(self, path: str | None) -> str:
        """Normalize client path to a relative path under the jail root."""
        if path is None or path == "" or path == ".":
            return "."
        raw = Path(path)
        if raw.is_absolute():
            try:
                rel = Path(os.path.realpath(path)).relative_to(self.root)
            except ValueError as exc:
                raise JailError(
                    "PATH_OUTSIDE_ROOT",
                    f"path {path!r} is outside authorized root {self.root}",
                ) from exc
            return "." if str(rel) == "." else str(rel)
        # relative: keep as posix relative (openat2 will enforce)
        # reject absolute-looking after expand tricks
        parts = Path(path).parts
        if parts and parts[0] == "/":
            raise JailError("PATH_OUTSIDE_ROOT", f"path {path!r} is absolute")
        return path

    def open(self, path: str | None, *, flags: int = os.O_RDONLY, directory: bool = False) -> int:
        rel = self.to_relative(path)
        fl = flags
        if directory:
            fl |= os.O_DIRECTORY
        how = _OpenHow(
            flags=fl,
            mode=0,
            resolve=RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS,
        )
        # empty relative "." for root
        rel_b = b"." if rel in {"", "."} else rel.encode("utf-8", errors="surrogateescape")
        ctypes.set_errno(0)
        fd = _libc.syscall(SYS_openat2, self._root_fd, rel_b, ctypes.byref(how), ctypes.sizeof(how))
        if fd < 0:
            err = ctypes.get_errno()
            if err in {errno.EXDEV, errno.EPERM}:
                raise JailError(
                    "PATH_OUTSIDE_ROOT",
                    f"path {path!r} escapes authorized root (openat2)",
                )
            if err == errno.ENOENT:
                raise JailError("NOT_FOUND", f"path not found: {path!r}")
            if err == errno.EACCES:
                raise JailError("PERMISSION_DENIED", f"permission denied: {path!r}")
            if err == errno.ENOTDIR and directory:
                raise JailError("INVALID_ARGUMENT", f"not a directory: {path!r}")
            raise JailError("INTERNAL_ERROR", f"openat2 failed for {path!r}: errno={err}")
        return int(fd)

    def display_path(self, path: str | None) -> str:
        rel = self.to_relative(path)
        if rel in {"", "."}:
            return str(self.root)
        return str(self.root / rel)
