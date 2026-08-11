"""Disk artifact store with opaque IDs, TTL, and quota."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_ROOT = "/var/lib/codeagent-mcp/artifacts"
DEFAULT_TTL_S = 3600
DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_GLOBAL_QUOTA = 80_000_000
DEFAULT_LEASE_QUOTA = 20_000_000
DEFAULT_MAX_PIXELS = 8_000_000


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    path: Path
    mime_type: str
    width: int
    height: int
    size_bytes: int
    created_at: float
    expires_at: float
    kind: str
    lease_id: str | None


class ArtifactStore:
    def __init__(
        self,
        root: Path | None = None,
        *,
        ttl_s: int = DEFAULT_TTL_S,
        max_bytes: int = DEFAULT_MAX_BYTES,
        global_quota: int = DEFAULT_GLOBAL_QUOTA,
        lease_quota: int = DEFAULT_LEASE_QUOTA,
    ) -> None:
        self.root = root or Path(os.environ.get("CODEAGENT_ARTIFACT_ROOT", DEFAULT_ROOT))
        self.ttl_s = int(os.environ.get("CODEAGENT_ARTIFACT_TTL_S", ttl_s))
        self.max_bytes = int(os.environ.get("CODEAGENT_ARTIFACT_MAX_BYTES", max_bytes))
        self.global_quota = int(os.environ.get("CODEAGENT_ARTIFACT_GLOBAL_QUOTA", global_quota))
        self.lease_quota = int(os.environ.get("CODEAGENT_ARTIFACT_LEASE_QUOTA", lease_quota))
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._index = self.root / "index.json"
        self.cleanup_expired()

    def _load_index(self) -> dict[str, Any]:
        if not self._index.exists():
            return {"artifacts": {}}
        try:
            data = json.loads(self._index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"artifacts": {}}
        if not isinstance(data, dict) or "artifacts" not in data:
            return {"artifacts": {}}
        return data

    def _save_index(self, data: dict[str, Any]) -> None:
        tmp = self._index.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self._index)

    def cleanup_expired(self) -> int:
        now = time.time()
        data = self._load_index()
        arts = dict(data.get("artifacts") or {})
        removed = 0
        for aid, meta in list(arts.items()):
            exp = float(meta.get("expires_at") or 0)
            path = self.root / f"{aid}.png"
            if exp and exp < now:
                path.unlink(missing_ok=True)
                arts.pop(aid, None)
                removed += 1
            elif not path.exists():
                arts.pop(aid, None)
                removed += 1
        data["artifacts"] = arts
        self._save_index(data)
        return removed

    def _used_bytes(self, arts: dict[str, Any]) -> int:
        total = 0
        for meta in arts.values():
            total += int(meta.get("size_bytes") or 0)
        return total

    def put_png(
        self,
        data: bytes,
        *,
        width: int,
        height: int,
        kind: str,
        lease_id: str | None,
    ) -> Artifact:
        self.cleanup_expired()
        if len(data) > self.max_bytes:
            raise ValueError(f"artifact exceeds max_bytes ({self.max_bytes})")
        if width * height > DEFAULT_MAX_PIXELS:
            raise ValueError(f"artifact exceeds max pixels ({DEFAULT_MAX_PIXELS})")
        data_idx = self._load_index()
        arts = dict(data_idx.get("artifacts") or {})
        used = self._used_bytes(arts)
        if used + len(data) > self.global_quota:
            raise RuntimeError("artifact global quota exceeded")
        if lease_id:
            lease_used = sum(
                int(m.get("size_bytes") or 0)
                for m in arts.values()
                if m.get("lease_id") == lease_id
            )
            if lease_used + len(data) > self.lease_quota:
                raise RuntimeError("artifact per-lease quota exceeded")
        aid = uuid.uuid4().hex
        path = self.root / f"{aid}.png"
        path.write_bytes(data)
        path.chmod(0o600)
        now = time.time()
        meta = {
            "artifact_id": aid,
            "mime_type": "image/png",
            "width": width,
            "height": height,
            "size_bytes": len(data),
            "created_at": now,
            "expires_at": now + self.ttl_s,
            "kind": kind,
            "lease_id": lease_id,
            "filename": path.name,
        }
        arts[aid] = meta
        data_idx["artifacts"] = arts
        self._save_index(data_idx)
        return Artifact(
            artifact_id=aid,
            path=path,
            mime_type="image/png",
            width=width,
            height=height,
            size_bytes=len(data),
            created_at=now,
            expires_at=meta["expires_at"],
            kind=kind,
            lease_id=lease_id,
        )

    def get(self, artifact_id: str) -> Artifact | None:
        self.cleanup_expired()
        if not artifact_id or ".." in artifact_id or "/" in artifact_id:
            return None
        if not all(c in "0123456789abcdef" for c in artifact_id):
            return None
        data = self._load_index()
        meta = (data.get("artifacts") or {}).get(artifact_id)
        if not meta:
            return None
        path = self.root / f"{artifact_id}.png"
        if not path.exists():
            return None
        return Artifact(
            artifact_id=artifact_id,
            path=path,
            mime_type=str(meta.get("mime_type") or "image/png"),
            width=int(meta.get("width") or 0),
            height=int(meta.get("height") or 0),
            size_bytes=int(meta.get("size_bytes") or path.stat().st_size),
            created_at=float(meta.get("created_at") or 0),
            expires_at=float(meta.get("expires_at") or 0),
            kind=str(meta.get("kind") or "png"),
            lease_id=meta.get("lease_id"),
        )

    def read_bytes(self, artifact_id: str) -> bytes | None:
        art = self.get(artifact_id)
        if art is None:
            return None
        return art.path.read_bytes()
