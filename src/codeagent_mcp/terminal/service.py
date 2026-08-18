"""Terminal lifecycle service."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codeagent_mcp.errors import tool_error, tool_ok
from codeagent_mcp.paths import resolve_under_root
from codeagent_mcp.terminal import spool, tmux
from codeagent_mcp.terminal.spool import (
    DEFAULT_MAX_READ_BYTES,
    DEFAULT_MAX_SPOOL_BYTES,
    CursorExpired,
)
from codeagent_mcp.terminal.store import TerminalStore, TerminalStoreError
from codeagent_mcp.tools.workspace import get_lease_manager

MAX_TERMINALS_PER_LEASE = 3
RESERVED_ALIASES = frozenset({"main", "app", "debug"})
ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
PANE_ID_RE = re.compile(r"^%\d+$")

KEY_MAP = {
    "ENTER": "Enter",
    "CTRL_C": "C-c",
    "CTRL_D": "C-d",
    "TAB": "Tab",
    "ESC": "Escape",
    "UP": "Up",
    "DOWN": "Down",
    "LEFT": "Left",
    "RIGHT": "Right",
}


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_alias(alias: str) -> str | None:
    if not alias or not ALIAS_RE.match(alias):
        return "alias must match ^[a-z][a-z0-9_-]{0,31}$"
    if alias.startswith("_"):
        return "alias must not start with underscore (reserved)"
    return None


class TerminalService:
    def __init__(self, store: TerminalStore | None = None) -> None:
        self.store = store or TerminalStore()

    def _require_lease(self, lease_id: str) -> dict[str, Any]:
        return get_lease_manager().require_active(lease_id=lease_id)

    @staticmethod
    def _lease_or_error(lease: dict[str, Any]) -> dict[str, Any] | None:
        if not lease.get("ok"):
            return lease
        return None

    def _reconcile(self, data: dict[str, Any]) -> dict[str, Any]:
        live = {p.pane_id: p for p in tmux.list_panes() if p.window_name != "_boot"}
        terminals = dict(data.get("terminals") or {})
        for pane_id, rec in list(terminals.items()):
            pane = live.get(pane_id)
            if pane is None:
                rec = {**rec, "alive": False, "session_dead": True}
                terminals[pane_id] = rec
            else:
                terminals[pane_id] = {
                    **rec,
                    "alive": not pane.pane_dead,
                    "session_dead": bool(pane.pane_dead),
                    "pane_pid": pane.pane_pid,
                    "pane_current_command": pane.pane_current_command,
                    "pane_current_path": pane.pane_current_path,
                }
        data = {**data, "terminals": terminals}
        return data

    def _find_by_ref(
        self, terminals: dict[str, Any], *, pane_id: str | None, alias: str | None
    ) -> tuple[str, dict[str, Any]] | None:
        if pane_id:
            rec = terminals.get(pane_id)
            if rec:
                return pane_id, rec
            return None
        if alias:
            matches = [(pid, r) for pid, r in terminals.items() if r.get("alias") == alias]
            if not matches:
                return None
            # Prefer alive
            for pid, r in matches:
                if r.get("alive") and not r.get("session_dead"):
                    return pid, r
            return matches[0]
        return None

    def _authorize_mutate(
        self, rec: dict[str, Any], lease: dict[str, Any], *, reclaim: bool = False
    ) -> dict[str, Any] | None:
        if rec.get("project") != lease.get("project"):
            return tool_error(
                "AUTHORIZATION_DENIED",
                "terminal belongs to a different project",
                retryable=False,
            )
        owner = rec.get("owner_lease_id")
        if owner == lease.get("lease_id"):
            return None
        if reclaim:
            # Active exclusive lease for same project may reclaim orphans.
            return None
        return tool_error(
            "AUTHORIZATION_DENIED",
            "terminal owned by a different lease; close/reset to reclaim",
            retryable=False,
            next_action="Call terminal_close or terminal_reset with this lease to reclaim",
        )

    def list(self, *, lease_id: str) -> dict[str, Any]:
        lease = self._require_lease(lease_id)
        if err := self._lease_or_error(lease):
            return err
        try:

            def _sync(d: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
                synced = self._reconcile(d)
                return synced, synced

            data = self.store.read_modify_write(_sync)
        except TerminalStoreError as exc:
            return tool_error("INTERNAL_ERROR", str(exc), retryable=True)
        items = []
        items_src = sorted(data["terminals"].items(), key=lambda kv: kv[1].get("alias", ""))
        for pane_id, rec in items_src:
            if rec.get("project") != lease["project"]:
                continue
            items.append(self._public_rec(pane_id, rec))
        return tool_ok(project=lease["project"], terminals=items, count=len(items))

    def status(
        self, *, lease_id: str, pane_id: str | None = None, alias: str | None = None
    ) -> dict[str, Any]:
        lease = self._require_lease(lease_id)
        if err := self._lease_or_error(lease):
            return err
        if not pane_id and not alias:
            return tool_error(
                "INVALID_ARGUMENT",
                "pane_id or alias is required",
                retryable=False,
            )
        if pane_id and not PANE_ID_RE.match(pane_id):
            return tool_error("INVALID_ARGUMENT", "pane_id must look like %N", retryable=False)
        try:

            def _sync(d: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
                synced = self._reconcile(d)
                return synced, synced

            data = self.store.read_modify_write(_sync)
        except TerminalStoreError as exc:
            return tool_error("INTERNAL_ERROR", str(exc), retryable=True)
        found = self._find_by_ref(data["terminals"], pane_id=pane_id, alias=alias)
        if not found:
            return tool_error(
                "NOT_FOUND",
                "unknown terminal",
                retryable=False,
                next_action="Call terminal_list or terminal_create",
            )
        pid, rec = found
        if rec.get("project") != lease["project"]:
            return tool_error("AUTHORIZATION_DENIED", "terminal project mismatch", retryable=False)
        if rec.get("session_dead") or not rec.get("alive", True):
            return tool_error(
                "SESSION_DEAD",
                f"terminal {rec.get('alias')} ({pid}) is dead",
                retryable=False,
                next_action="Call terminal_reset or terminal_create",
                pane_id=pid,
                alias=rec.get("alias"),
            )
        pub = self._public_rec(pid, rec)
        pub["status"] = "alive"
        return tool_ok(**pub)

    def create(
        self,
        *,
        lease_id: str,
        alias: str,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        lease = self._require_lease(lease_id)
        if err := self._lease_or_error(lease):
            return err
        err = _validate_alias(alias)
        if err:
            return tool_error("INVALID_ARGUMENT", err, retryable=False)
        if problem := tmux.socket_path_problem():
            # Reported before tmux is invoked: retrying this never succeeds, and
            # the previous INTERNAL_ERROR invited exactly that.
            return tool_error(
                "INVALID_ARGUMENT",
                problem,
                retryable=False,
                next_action="Set CODEAGENT_TMUX_SOCKET to a shorter path and restart",
            )
        root = lease["root"]
        try:
            workdir = str(resolve_under_root(cwd or root, root))
        except ValueError as exc:
            return tool_error("PATH_OUTSIDE_ROOT", str(exc), retryable=False)

        def mutator(data: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
            data = self._reconcile(data)
            terminals = dict(data["terminals"])
            # Reap dead/missing panes for this alias so create cannot accumulate zombies.
            for pid, r in list(terminals.items()):
                if r.get("alias") != alias:
                    continue
                if r.get("alive") and not r.get("session_dead"):
                    return (
                        tool_error(
                            "CONFLICT",
                            f"alias {alias!r} already bound to {pid}",
                            retryable=False,
                            next_action="Use terminal_reset or choose another alias",
                        ),
                        data,
                    )
                tmux.pipe_pane_detach(pid)
                spool.delete_spool(r.get("spool_path"))
                tmux.kill_pane(pid)
                terminals.pop(pid, None)
            data = {**data, "terminals": terminals}
            owned = [
                r
                for r in terminals.values()
                if r.get("owner_lease_id") == lease["lease_id"] and r.get("alive")
            ]
            if len(owned) >= MAX_TERMINALS_PER_LEASE:
                return (
                    tool_error(
                        "RISK_BLOCKED",
                        f"max {MAX_TERMINALS_PER_LEASE} terminals per lease",
                        retryable=False,
                    ),
                    data,
                )
            try:
                pane = tmux.create_window(alias=alias, cwd=workdir)
            except tmux.TmuxError as exc:
                return (
                    tool_error(
                        "INTERNAL_ERROR",
                        f"tmux create failed: {exc.stderr or exc}",
                        retryable=True,
                    ),
                    data,
                )
            gen = spool.new_generation()
            spool_path = spool.spool_path_for(gen)
            spool.ensure_spool_file(spool_path)
            try:
                tmux.pipe_pane_attach(pane.pane_id, spool_path)
            except tmux.TmuxError as exc:
                tmux.kill_pane(pane.pane_id)
                spool.delete_spool(spool_path)
                return (
                    tool_error(
                        "INTERNAL_ERROR",
                        f"pipe-pane attach failed: {exc.stderr or exc}",
                        retryable=True,
                    ),
                    data,
                )
            rec = {
                "pane_id": pane.pane_id,
                "alias": alias,
                "project": lease["project"],
                "owner_lease_id": lease["lease_id"],
                "cwd": workdir,
                "created_at": _now_iso(),
                "alive": not pane.pane_dead,
                "session_dead": bool(pane.pane_dead),
                "pane_pid": pane.pane_pid,
                "pane_current_command": pane.pane_current_command,
                "pane_current_path": pane.pane_current_path,
                "spool_generation": gen,
                "spool_path": str(spool_path),
                "spool_byte_base": 0,
            }
            terminals[pane.pane_id] = rec
            data = {**data, "terminals": terminals}
            # Report the provisioned temp root: a pane shell inherits it, but a
            # caller that does not know its path falls back to /tmp, which a
            # hardened unit gives it no access to.
            return (
                tool_ok(
                    **self._public_rec(pane.pane_id, rec),
                    tmpdir=str(tmux.codeagent_tmpdir()),
                ),
                data,
            )

        try:
            return self.store.read_modify_write(mutator)
        except TerminalStoreError as exc:
            return tool_error("INTERNAL_ERROR", str(exc), retryable=True)

    def write(
        self,
        *,
        lease_id: str,
        text: str,
        pane_id: str | None = None,
        alias: str | None = None,
    ) -> dict[str, Any]:
        if text is None or not isinstance(text, str):
            return tool_error("INVALID_ARGUMENT", "text must be a string", retryable=False)
        if len(text.encode("utf-8")) > 64_000:
            return tool_error("INVALID_ARGUMENT", "text exceeds 64KiB", retryable=False)
        return self._with_live_pane(
            lease_id=lease_id,
            pane_id=pane_id,
            alias=alias,
            reclaim=False,
            action=lambda pid: tmux.send_literal(pid, text),
            ok_extra={"bytes": len(text.encode("utf-8"))},
        )

    def key(
        self, *, lease_id: str, key: str, pane_id: str | None = None, alias: str | None = None
    ) -> dict[str, Any]:
        if key not in KEY_MAP:
            return tool_error(
                "INVALID_ARGUMENT",
                f"key must be one of {sorted(KEY_MAP)}",
                retryable=False,
            )
        return self._with_live_pane(
            lease_id=lease_id,
            pane_id=pane_id,
            alias=alias,
            reclaim=False,
            action=lambda pid: tmux.send_key(pid, KEY_MAP[key]),
            ok_extra={"key": key},
        )

    def interrupt(
        self, *, lease_id: str, pane_id: str | None = None, alias: str | None = None
    ) -> dict[str, Any]:
        return self._with_live_pane(
            lease_id=lease_id,
            pane_id=pane_id,
            alias=alias,
            reclaim=False,
            action=lambda pid: tmux.send_key(pid, KEY_MAP["CTRL_C"]),
            ok_extra={"signal": "SIGINT_via_tty"},
        )

    def close(
        self, *, lease_id: str, pane_id: str | None = None, alias: str | None = None
    ) -> dict[str, Any]:
        lease = self._require_lease(lease_id)
        if err := self._lease_or_error(lease):
            return err

        def mutator(data: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
            data = self._reconcile(data)
            found = self._find_by_ref(data["terminals"], pane_id=pane_id, alias=alias)
            if not found:
                return (
                    tool_error("NOT_FOUND", "unknown terminal", retryable=False),
                    data,
                )
            pid, rec = found
            denied = self._authorize_mutate(rec, lease, reclaim=True)
            if denied:
                return denied, data
            tmux.pipe_pane_detach(pid)
            spool.delete_spool(rec.get("spool_path"))
            tmux.kill_pane(pid)
            terminals = dict(data["terminals"])
            terminals.pop(pid, None)
            return (
                tool_ok(status="closed", pane_id=pid, alias=rec.get("alias")),
                {**data, "terminals": terminals},
            )

        try:
            return self.store.read_modify_write(mutator)
        except TerminalStoreError as exc:
            return tool_error("INTERNAL_ERROR", str(exc), retryable=True)

    def reset(self, *, lease_id: str, alias: str, cwd: str | None = None) -> dict[str, Any]:
        closed = self.close(lease_id=lease_id, alias=alias)
        if not closed.get("ok") and closed.get("error", {}).get("code") not in {
            "NOT_FOUND",
            "SESSION_DEAD",
        }:
            # NOT_FOUND is fine — create fresh
            if closed.get("error", {}).get("code") != "NOT_FOUND":
                return closed
        return self.create(lease_id=lease_id, alias=alias, cwd=cwd)

    def read(
        self,
        *,
        lease_id: str,
        cursor: str | None = None,
        max_bytes: int = DEFAULT_MAX_READ_BYTES,
        pane_id: str | None = None,
        alias: str | None = None,
    ) -> dict[str, Any]:
        lease = self._require_lease(lease_id)
        if err := self._lease_or_error(lease):
            return err
        if not pane_id and not alias:
            return tool_error("INVALID_ARGUMENT", "pane_id or alias is required", retryable=False)
        if pane_id and not PANE_ID_RE.match(pane_id):
            return tool_error("INVALID_ARGUMENT", "pane_id must look like %N", retryable=False)
        try:
            decoded = spool.Cursor.decode(cursor)
        except ValueError as exc:
            return tool_error("INVALID_ARGUMENT", str(exc), retryable=False)

        def mutator(data: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
            data = self._reconcile(data)
            found = self._find_by_ref(data["terminals"], pane_id=pane_id, alias=alias)
            if not found:
                return tool_error("NOT_FOUND", "unknown terminal", retryable=False), data
            pid, rec = found
            denied = self._authorize_mutate(rec, lease, reclaim=False)
            if denied:
                return denied, data
            if not rec.get("spool_path") or not rec.get("spool_generation"):
                return (
                    tool_error(
                        "INTERNAL_ERROR",
                        "terminal has no spool (recreate with terminal_reset)",
                        retryable=False,
                    ),
                    data,
                )
            # Re-ensure pipe after MCP restart (idempotent -O).
            try:
                self._ensure_pipe(pid, rec)
            except tmux.TmuxError as exc:
                return (
                    tool_error(
                        "INTERNAL_ERROR",
                        f"pipe-pane ensure failed: {exc.stderr or exc}",
                        retryable=True,
                    ),
                    data,
                )

            path = Path(rec["spool_path"])
            gen = rec["spool_generation"]
            base = int(rec.get("spool_byte_base") or 0)

            # Rotate if oversized (stop pipe → replace → restart).
            if spool.physical_size(path) >= DEFAULT_MAX_SPOOL_BYTES:
                tmux.pipe_pane_detach(pid)
                new_base, rotated = spool.rotate_file(path=path, byte_base=base)
                if rotated:
                    base = new_base
                    rec = {**rec, "spool_byte_base": base}
                    terminals = dict(data["terminals"])
                    terminals[pid] = rec
                    data = {**data, "terminals": terminals}
                try:
                    tmux.pipe_pane_attach(pid, path)
                except tmux.TmuxError as exc:
                    return (
                        tool_error(
                            "INTERNAL_ERROR",
                            f"pipe-pane re-attach after rotate failed: {exc.stderr or exc}",
                            retryable=True,
                        ),
                        data,
                    )

            try:
                chunk = spool.read_spool(
                    path=path,
                    generation=gen,
                    byte_base=base,
                    cursor=decoded,
                    max_bytes=max_bytes,
                )
            except CursorExpired as exc:
                start_cur = spool.Cursor(generation=exc.generation, offset=exc.retained_start)
                return (
                    tool_error(
                        "CURSOR_EXPIRED",
                        str(exc),
                        retryable=True,
                        next_action=(
                            f"Retry terminal_read with cursor={start_cur.encode()!r} "
                            "or omit cursor to read from retained start"
                        ),
                        retained_cursor=start_cur.encode(),
                    ),
                    data,
                )
            except OSError as exc:
                return tool_error(
                    "INTERNAL_ERROR", f"spool read failed: {exc}", retryable=True
                ), data

            if chunk.get("binary_suspected"):
                return (
                    tool_error(
                        "UNSUPPORTED_BINARY",
                        "spool chunk contains NUL bytes; not returned as text",
                        retryable=False,
                        next_action="Use terminal_snapshot or inspect via exec_run",
                        next_cursor=chunk["next_cursor"],
                        raw_byte_len=chunk["raw_byte_len"],
                    ),
                    data,
                )

            pub = self._public_rec(pid, rec)
            return (
                tool_ok(
                    **pub,
                    text=chunk["text"],
                    raw_byte_len=chunk["raw_byte_len"],
                    cursor=decoded.encode() if decoded else None,
                    next_cursor=chunk["next_cursor"],
                    has_more=chunk["has_more"],
                    truncated=chunk["truncated"],
                ),
                data,
            )

        try:
            return self.store.read_modify_write(mutator)
        except TerminalStoreError as exc:
            return tool_error("INTERNAL_ERROR", str(exc), retryable=True)

    def snapshot(
        self,
        *,
        lease_id: str,
        pane_id: str | None = None,
        alias: str | None = None,
        include_history: bool = True,
    ) -> dict[str, Any]:
        lease = self._require_lease(lease_id)
        if err := self._lease_or_error(lease):
            return err
        if not pane_id and not alias:
            return tool_error("INVALID_ARGUMENT", "pane_id or alias is required", retryable=False)

        def mutator(data: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
            data = self._reconcile(data)
            found = self._find_by_ref(data["terminals"], pane_id=pane_id, alias=alias)
            if not found:
                return tool_error("NOT_FOUND", "unknown terminal", retryable=False), data
            pid, rec = found
            denied = self._authorize_mutate(rec, lease, reclaim=False)
            if denied:
                return denied, data
            if rec.get("session_dead") or not rec.get("alive", True):
                return (
                    tool_error(
                        "SESSION_DEAD",
                        f"terminal {rec.get('alias')} ({pid}) is dead",
                        retryable=False,
                        next_action="Call terminal_reset",
                    ),
                    data,
                )
            try:
                raw = tmux.capture_pane_snapshot(pid, include_history=include_history)
            except tmux.TmuxError as exc:
                return (
                    tool_error(
                        "INTERNAL_ERROR",
                        f"capture-pane failed: {exc.stderr or exc}",
                        retryable=True,
                    ),
                    data,
                )
            text, _ = spool.sanitize_for_client(raw.encode("utf-8", errors="replace"))
            pub = self._public_rec(pid, rec)
            return tool_ok(**pub, text=text, source="capture-pane"), data

        try:
            return self.store.read_modify_write(mutator)
        except TerminalStoreError as exc:
            return tool_error("INTERNAL_ERROR", str(exc), retryable=True)

    @staticmethod
    def _ensure_pipe(pane_id: str, rec: dict[str, Any]) -> None:
        """Re-attach pipe only when missing (Gate D: do not bounce an active pipe)."""
        path = Path(rec["spool_path"])
        spool.ensure_spool_file(path)
        if tmux.pane_pipe_active(pane_id):
            return
        tmux.pipe_pane_attach(pane_id, path)

    def _with_live_pane(
        self,
        *,
        lease_id: str,
        pane_id: str | None,
        alias: str | None,
        reclaim: bool,
        action,
        ok_extra: dict[str, Any],
    ) -> dict[str, Any]:
        lease = self._require_lease(lease_id)
        if err := self._lease_or_error(lease):
            return err
        if not pane_id and not alias:
            return tool_error("INVALID_ARGUMENT", "pane_id or alias is required", retryable=False)
        if pane_id and not PANE_ID_RE.match(pane_id):
            return tool_error("INVALID_ARGUMENT", "pane_id must look like %N", retryable=False)

        def mutator(data: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
            data = self._reconcile(data)
            found = self._find_by_ref(data["terminals"], pane_id=pane_id, alias=alias)
            if not found:
                return tool_error("NOT_FOUND", "unknown terminal", retryable=False), data
            pid, rec = found
            denied = self._authorize_mutate(rec, lease, reclaim=reclaim)
            if denied:
                return denied, data
            if rec.get("session_dead") or not rec.get("alive", True):
                return (
                    tool_error(
                        "SESSION_DEAD",
                        f"terminal {rec.get('alias')} ({pid}) is dead",
                        retryable=False,
                        next_action="Call terminal_reset",
                    ),
                    data,
                )
            try:
                action(pid)
            except tmux.TmuxError as exc:
                return (
                    tool_error(
                        "INTERNAL_ERROR",
                        f"tmux op failed: {exc.stderr or exc}",
                        retryable=True,
                    ),
                    data,
                )
            pub = self._public_rec(pid, rec)
            return tool_ok(**pub, **ok_extra), data

        try:
            return self.store.read_modify_write(mutator)
        except TerminalStoreError as exc:
            return tool_error("INTERNAL_ERROR", str(exc), retryable=True)

    @staticmethod
    def _public_rec(pane_id: str, rec: dict[str, Any]) -> dict[str, Any]:
        return {
            "pane_id": pane_id,
            "alias": rec.get("alias"),
            "project": rec.get("project"),
            "cwd": rec.get("cwd"),
            "alive": bool(rec.get("alive")),
            "session_dead": bool(rec.get("session_dead")),
            "pane_pid": rec.get("pane_pid"),
            "pane_current_command": rec.get("pane_current_command"),
            "pane_current_path": rec.get("pane_current_path"),
            "created_at": rec.get("created_at"),
        }


def poll_until(predicate, *, timeout_s: float = 5.0, interval_s: float = 0.05) -> bool:
    """Test helper: poll without fixed sleeps as the only wait strategy."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return False
