"""Atomic JSON lease store with exclusive flock (fail-closed on corruption)."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class LeaseStoreError(RuntimeError):
    """Store IO/parse failure — treat as fail-closed, not free."""


class LeaseStore:
    """Persist a single-document lease map under an exclusive lockfile."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read_modify_write(self, mutator) -> Any:
        """Hold LOCK_EX for the whole check→mutate→write cycle."""
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
            return {"schema_version": SCHEMA_VERSION, "leases": {}}
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise LeaseStoreError(f"lease store unreadable: {exc}") from exc
        if not isinstance(data, dict) or "leases" not in data:
            raise LeaseStoreError("lease store missing leases map")
        if not isinstance(data["leases"], dict):
            raise LeaseStoreError("lease store leases is not an object")
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
            # fsync directory for durability of rename
            dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            raise LeaseStoreError(f"lease store write failed: {exc}") from exc
