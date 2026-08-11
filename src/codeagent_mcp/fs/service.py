"""Filesystem service: stat/list/read/search under PathJail."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from codeagent_mcp.errors import tool_error, tool_ok
from codeagent_mcp.fs.binary import is_binary
from codeagent_mcp.fs.binary_write import write_binary as apply_binary_write
from codeagent_mcp.fs.binary_write import write_bytes as apply_bytes_write
from codeagent_mcp.fs.openat2 import JailError, PathJail
from codeagent_mcp.fs.patch import apply_structured_patch
from codeagent_mcp.workspace.projects import get_project, known_projects

DEFAULT_MAX_READ_BYTES = 200_000
DEFAULT_MAX_LIST = 500
DEFAULT_MAX_SEARCH_MATCHES = 100
HARD_MAX_READ_BYTES = 2_000_000
SEARCH_TIMEOUT_S = 30


class FsService:
    def __init__(self, *, project: str = "demo", root: str | None = None) -> None:
        if root is not None:
            self.project = project
            self.root = root
        else:
            cfg = get_project(project)
            if cfg is None:
                raise ValueError(f"unknown project {project!r}")
            self.project = cfg.name
            self.root = cfg.root

    def _jail(self) -> PathJail:
        return PathJail(self.root)

    @staticmethod
    def _from_jail(exc: JailError) -> dict[str, Any]:
        return tool_error(exc.code, exc.message, retryable=False)

    def stat(self, path: str = "") -> dict[str, Any]:
        try:
            with self._jail() as jail:
                fd = jail.open(path)
                try:
                    st = os.fstat(fd)
                    kind = "dir" if os.fstat(fd).st_mode & 0o170000 == 0o040000 else "file"
                    # refine with S_ISDIR
                    import stat as statmod

                    mode = st.st_mode
                    if statmod.S_ISDIR(mode):
                        kind = "dir"
                    elif statmod.S_ISLNK(mode):
                        kind = "symlink"
                    elif statmod.S_ISREG(mode):
                        kind = "file"
                    else:
                        kind = "other"
                    return tool_ok(
                        project=self.project,
                        path=jail.display_path(path),
                        relative=jail.to_relative(path),
                        type=kind,
                        size_bytes=st.st_size,
                        mtime=st.st_mtime,
                        mode_octal=oct(statmod.S_IMODE(mode)),
                    )
                finally:
                    os.close(fd)
        except JailError as exc:
            return self._from_jail(exc)
        except ValueError as exc:
            return tool_error("INVALID_ARGUMENT", str(exc), retryable=False)

    def list_dir(self, path: str = "", *, max_entries: int = DEFAULT_MAX_LIST) -> dict[str, Any]:
        if max_entries < 1 or max_entries > 5000:
            return tool_error(
                "INVALID_ARGUMENT",
                "max_entries must be in [1, 5000]",
                retryable=False,
            )
        try:
            with self._jail() as jail:
                fd = jail.open(path, directory=True)
                try:
                    # Enumerate via the already-confined fd (proc fd path is our descriptor).
                    names = sorted(os.listdir(f"/proc/self/fd/{fd}"))
                    truncated = len(names) > max_entries
                    names = names[:max_entries]
                    entries: list[dict[str, Any]] = []
                    for name in names:
                        child_rel = (
                            name
                            if jail.to_relative(path) in {"", "."}
                            else str(Path(jail.to_relative(path)) / name)
                        )
                        try:
                            cfd = jail.open(child_rel)
                            try:
                                import stat as statmod

                                st = os.fstat(cfd)
                                if statmod.S_ISDIR(st.st_mode):
                                    et = "dir"
                                elif statmod.S_ISREG(st.st_mode):
                                    et = "file"
                                else:
                                    et = "other"
                                entries.append({"name": name, "type": et, "size_bytes": st.st_size})
                            finally:
                                os.close(cfd)
                        except JailError:
                            entries.append({"name": name, "type": "unknown", "size_bytes": None})
                    return tool_ok(
                        project=self.project,
                        path=jail.display_path(path),
                        relative=jail.to_relative(path),
                        entries=entries,
                        truncated=truncated,
                        count=len(entries),
                    )
                finally:
                    os.close(fd)
        except JailError as exc:
            return self._from_jail(exc)

    def read(
        self,
        path: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        max_bytes: int = DEFAULT_MAX_READ_BYTES,
    ) -> dict[str, Any]:
        if start_line < 1:
            return tool_error("INVALID_ARGUMENT", "start_line must be >= 1", retryable=False)
        if end_line is not None and end_line < start_line:
            return tool_error(
                "INVALID_ARGUMENT",
                "end_line must be >= start_line",
                retryable=False,
            )
        if max_bytes < 1 or max_bytes > HARD_MAX_READ_BYTES:
            return tool_error(
                "INVALID_ARGUMENT",
                f"max_bytes must be in [1, {HARD_MAX_READ_BYTES}]",
                retryable=False,
            )
        try:
            with self._jail() as jail:
                fd = jail.open(path)
                try:
                    import stat as statmod

                    st = os.fstat(fd)
                    if not statmod.S_ISREG(st.st_mode):
                        return tool_error(
                            "INVALID_ARGUMENT",
                            "fs_read requires a regular file",
                            retryable=False,
                        )
                    # sha256 full file
                    digest = hashlib.sha256()
                    os.lseek(fd, 0, os.SEEK_SET)
                    chunks: list[bytes] = []
                    total = 0
                    while True:
                        block = os.read(fd, 1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
                        # keep content only up to a safety cap for decoding (2x hard max)
                        if total < HARD_MAX_READ_BYTES * 2:
                            need = HARD_MAX_READ_BYTES * 2 - total
                            chunks.append(block[:need])
                            total += len(block[:need])
                    raw = b"".join(chunks)
                    if is_binary(raw):
                        return tool_error(
                            "UNSUPPORTED_BINARY",
                            "file looks binary; refuse to return as text",
                            retryable=False,
                            path=jail.display_path(path),
                            sha256=digest.hexdigest(),
                            size_bytes=st.st_size,
                        )
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        return tool_error(
                            "UNSUPPORTED_BINARY",
                            "file is not valid UTF-8 text",
                            retryable=False,
                            path=jail.display_path(path),
                            sha256=digest.hexdigest(),
                        )
                    lines = text.splitlines(keepends=True)
                    # If file was larger than we buffered, report limit
                    if st.st_size > len(raw):
                        return tool_error(
                            "OUTPUT_LIMIT",
                            "file too large to load for line-oriented read",
                            retryable=False,
                            next_action="Request a smaller line range or raise max carefully",
                            size_bytes=st.st_size,
                            sha256=digest.hexdigest(),
                        )
                    last = end_line if end_line is not None else len(lines)
                    last = min(last, len(lines))
                    slice_lines = lines[start_line - 1 : last]
                    content = "".join(slice_lines)
                    truncated = False
                    if len(content.encode("utf-8")) > max_bytes:
                        # truncate on byte boundary
                        encoded = content.encode("utf-8")[:max_bytes]
                        content = encoded.decode("utf-8", errors="ignore")
                        truncated = True
                    return tool_ok(
                        project=self.project,
                        path=jail.display_path(path),
                        relative=jail.to_relative(path),
                        sha256=digest.hexdigest(),
                        start_line=start_line,
                        end_line=(
                            start_line - 1 + len(slice_lines) if slice_lines else start_line - 1
                        ),
                        total_lines=len(lines),
                        content=content,
                        truncated=truncated,
                        size_bytes=st.st_size,
                    )
                finally:
                    os.close(fd)
        except JailError as exc:
            return self._from_jail(exc)

    def search(
        self,
        query: str,
        *,
        path: str = "",
        literal: bool = False,
        max_matches: int = DEFAULT_MAX_SEARCH_MATCHES,
        glob: str = "",
    ) -> dict[str, Any]:
        if not query:
            return tool_error("INVALID_ARGUMENT", "query must be non-empty", retryable=False)
        if max_matches < 1 or max_matches > 1000:
            return tool_error(
                "INVALID_ARGUMENT",
                "max_matches must be in [1, 1000]",
                retryable=False,
            )
        rg = shutil.which("rg")
        if not rg:
            return tool_error(
                "INTERNAL_ERROR",
                "ripgrep (rg) is not installed on PATH",
                retryable=False,
                next_action="Install ripgrep on the CodeAgent host",
            )
        if not literal:
            try:
                re.compile(query)
            except re.error as exc:
                return tool_error(
                    "INVALID_ARGUMENT",
                    f"invalid regex: {exc}",
                    retryable=False,
                )
        try:
            with self._jail() as jail:
                # ensure search root is confined directory
                dir_fd = jail.open(path, directory=True)
                os.close(dir_fd)
                search_dir = jail.display_path(path)
                cmd = [
                    rg,
                    "--line-number",
                    "--no-heading",
                    "--color",
                    "never",
                    "--max-count",
                    str(max_matches),
                    # do not follow symlinks
                    "--glob",
                    "!.git/",
                ]
                if glob:
                    cmd.extend(["--glob", glob])
                if literal:
                    cmd.append("--fixed-strings")
                cmd.extend(["--", query, "."])
                started = time.monotonic()
                try:
                    proc = subprocess.run(  # noqa: S603
                        cmd,
                        cwd=search_dir,
                        capture_output=True,
                        text=True,
                        timeout=SEARCH_TIMEOUT_S,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    return tool_error(
                        "TIMEOUT",
                        f"rg exceeded {SEARCH_TIMEOUT_S}s",
                        retryable=True,
                    )
                duration_ms = int((time.monotonic() - started) * 1000)
                # exit 0 = matches, 1 = no matches, 2 = error
                if proc.returncode not in {0, 1}:
                    err = (proc.stderr or "").strip() or "rg failed"
                    return tool_error(
                        "INVALID_ARGUMENT" if "regex" in err.lower() else "INTERNAL_ERROR",
                        err[:500],
                        retryable=False,
                    )
                matches: list[dict[str, Any]] = []
                for line in (proc.stdout or "").splitlines():
                    # format: path:line:text
                    if ":" not in line:
                        continue
                    file_part, rest = line.split(":", 1)
                    if ":" not in rest:
                        continue
                    line_no_s, text = rest.split(":", 1)
                    try:
                        line_no = int(line_no_s)
                    except ValueError:
                        continue
                    matches.append(
                        {
                            "path": str(Path(search_dir) / file_part),
                            "relative": file_part,
                            "line": line_no,
                            "text": text[:500],
                        }
                    )
                    if len(matches) >= max_matches:
                        break
                truncated = len(matches) >= max_matches and proc.returncode == 0
                return tool_ok(
                    project=self.project,
                    path=search_dir,
                    query=query,
                    literal=literal,
                    matches=matches,
                    count=len(matches),
                    truncated=truncated,
                    duration_ms=duration_ms,
                )
        except JailError as exc:
            return self._from_jail(exc)

    def apply_patch(
        self,
        path: str,
        *,
        expected_sha256: str = "",
        edits: list[dict[str, str]] | None = None,
        new_content: str | None = None,
        create: bool = False,
    ) -> dict[str, Any]:
        result = apply_structured_patch(
            root=self.root,
            path=path,
            expected_sha256=expected_sha256,
            edits=edits,
            new_content=new_content,
            create=create,
        )
        if result.get("ok") is True:
            result["project"] = self.project
        return result

    def write_binary(
        self,
        path: str,
        *,
        content_base64: str,
        expected_sha256: str = "",
        create: bool = False,
    ) -> dict[str, Any]:
        result = apply_binary_write(
            root=self.root,
            path=path,
            content_base64=content_base64,
            expected_sha256=expected_sha256,
            create=create,
        )
        if result.get("ok") is True:
            result["project"] = self.project
        return result

    def write_bytes(
        self,
        path: str,
        *,
        data: bytes,
        expected_sha256: str = "",
        create: bool = False,
    ) -> dict[str, Any]:
        result = apply_bytes_write(
            root=self.root,
            path=path,
            data=data,
            expected_sha256=expected_sha256,
            create=create,
        )
        if result.get("ok") is True:
            result["project"] = self.project
        return result


def project_or_error(project: str) -> FsService | dict[str, Any]:
    if get_project(project) is None:
        return tool_error(
            "INVALID_ARGUMENT",
            f"unknown project {project!r}; known={list(known_projects())}",
            retryable=False,
        )
    return FsService(project=project)
