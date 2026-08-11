"""Safe cleanup: artifacts, spool TTL, orphan terminal/lease detection."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from codeagent_mcp.audit import emit_audit

DEFAULT_SPOOL_ROOT = "/var/lib/codeagent-mcp/spool"
DEFAULT_SPOOL_TTL_S = 86_400
DEFAULT_TERMINAL_STORE = "/var/lib/codeagent-mcp/terminals.json"
DEFAULT_LEASE_STORE = "/var/lib/codeagent-mcp/leases.json"


def cleanup_spool(
    root: Path | None = None,
    *,
    ttl_s: int | None = None,
) -> dict[str, int]:
    """Delete spool files older than TTL (by mtime)."""
    spool = root or Path(os.environ.get("CODEAGENT_SPOOL_ROOT", DEFAULT_SPOOL_ROOT))
    ttl = int(ttl_s or os.environ.get("CODEAGENT_SPOOL_TTL_S", DEFAULT_SPOOL_TTL_S))
    now = time.time()
    removed = 0
    bytes_freed = 0
    if not spool.exists():
        return {"removed": 0, "bytes_freed": 0}
    for path in spool.rglob("*"):
        if not path.is_file():
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        if now - st.st_mtime > ttl:
            try:
                bytes_freed += st.st_size
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
    emit_audit(
        {
            "event": "cleanup_spool",
            "ok": True,
            "removed": removed,
            "bytes_freed": bytes_freed,
            "ttl_s": ttl,
        }
    )
    return {"removed": removed, "bytes_freed": bytes_freed}


def detect_orphans(
    *,
    lease_store: Path | None = None,
    terminal_store: Path | None = None,
) -> dict[str, Any]:
    """Report lease↔terminal mismatches (detection only; no destructive cleanup)."""
    leases_path = lease_store or Path(os.environ.get("CODEAGENT_LEASE_STORE", DEFAULT_LEASE_STORE))
    terms_path = terminal_store or Path(
        os.environ.get("CODEAGENT_TERMINAL_STORE", DEFAULT_TERMINAL_STORE)
    )
    now = time.time()
    leases: dict[str, Any] = {}
    terminals: dict[str, Any] = {}
    if leases_path.exists():
        try:
            raw = json.loads(leases_path.read_text(encoding="utf-8"))
            leases = raw.get("leases") or raw if isinstance(raw, dict) else {}
            if "leases" in (raw or {}):
                leases = raw["leases"]
        except (OSError, json.JSONDecodeError):
            leases = {}
    if terms_path.exists():
        try:
            raw = json.loads(terms_path.read_text(encoding="utf-8"))
            terminals = raw.get("terminals") or raw if isinstance(raw, dict) else {}
            if isinstance(raw, dict) and "terminals" in raw:
                terminals = raw["terminals"]
        except (OSError, json.JSONDecodeError):
            terminals = {}

    active_lease_projects: set[str] = set()
    for rec in leases.values() if isinstance(leases, dict) else []:
        if not isinstance(rec, dict):
            continue
        exp = rec.get("expires_at")
        # expires_at may be ISO string; treat missing as active
        if isinstance(exp, (int, float)) and exp < now:
            continue
        if isinstance(exp, str):
            # lazy: if parse fails, keep
            try:
                from datetime import datetime

                if datetime.fromisoformat(exp.replace("Z", "+00:00")).timestamp() < now:
                    continue
            except ValueError:
                pass
        proj = rec.get("project")
        if isinstance(proj, str):
            active_lease_projects.add(proj)

    term_projects: set[str] = set()
    for rec in terminals.values() if isinstance(terminals, dict) else []:
        if isinstance(rec, dict) and isinstance(rec.get("project"), str):
            term_projects.add(rec["project"])

    lease_without_terms = sorted(active_lease_projects - term_projects)
    terms_without_lease = sorted(term_projects - active_lease_projects)
    result = {
        "ok": True,
        "active_lease_projects": sorted(active_lease_projects),
        "terminal_projects": sorted(term_projects),
        "lease_without_terminals": lease_without_terms,
        "terminals_without_lease": terms_without_lease,
        "orphan_hint_count": len(lease_without_terms) + len(terms_without_lease),
    }
    emit_audit(
        {"event": "orphan_detect", **{k: v for k, v in result.items() if k != "ok"}, "ok": True}
    )
    return result


def run_startup_cleanup() -> dict[str, Any]:
    """Idempotent cleanup suitable for process start."""
    from codeagent_mcp.artifact_store.store import ArtifactStore

    arts = ArtifactStore()
    removed_arts = arts.cleanup_expired()
    spool = cleanup_spool()
    orphans = detect_orphans()
    return {
        "artifacts_removed": removed_arts,
        "spool": spool,
        "orphans": orphans,
    }
