"""Atomic JSON terminal registry with exclusive flock."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_STORE = "/var/lib/codeagent-mcp/terminals.json"


class TerminalStoreError(RuntimeError):
    """Store IO/parse failure — fail-closed."""


class TerminalStore:
    """Persist alias↔pane registry under LOCK_EX."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(os.environ.get("CODEAGENT_TERMINAL_STORE", DEFAULT_STORE))
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read_modify_write(
        self, mutator: Callable[[dict[str, Any]], tuple[Any, dict[str, Any]]]
    ) -> Any:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+", encoding="utf-8") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                data = self._load_unlocked()
                result, new_data = mutator(data)
                self._save_unlocked(new_data)
                return result
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)

    def load(self) -> dict[str, Any]:
        with open(self.lock_path, "a+", encoding="utf-8") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_SH)
            try:
                return self._load_unlocked()
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": SCHEMA_VERSION, "terminals": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TerminalStoreError(f"terminal store unreadable: {exc}") from exc
        if not isinstance(data, dict) or "terminals" not in data:
            raise TerminalStoreError("terminal store missing terminals map")
        if not isinstance(data["terminals"], dict):
            raise TerminalStoreError("terminal store terminals is not an object")
        return data

    def _save_unlocked(self, data: dict[str, Any]) -> None:
        data = {**data, "schema_version": SCHEMA_VERSION}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            raise TerminalStoreError(f"terminal store write failed: {exc}") from exc
