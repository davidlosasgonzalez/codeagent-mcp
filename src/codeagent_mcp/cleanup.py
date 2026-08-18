"""Safe cleanup: artifacts, spool TTL, orphan terminal/lease detection."""

from __future__ import annotations

import json
import os
import signal
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


# A browser bundle lives under this root; a chrome process launched from
# anywhere else is not ours to touch.
DEFAULT_BROWSERS_ROOT = "/var/lib/codeagent-mcp/playwright"

# Below this age a detached browser is probably still starting up.
ORPHAN_BROWSER_MIN_AGE_S = 600


def _proc_field(pid: int, name: str) -> str:
    try:
        return (Path("/proc") / str(pid) / name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def find_orphan_browsers(
    *,
    browsers_root: str | None = None,
    min_age_s: int = ORPHAN_BROWSER_MIN_AGE_S,
) -> list[dict[str, Any]]:
    """Detached browser processes launched from our bundle.

    Detached means reparented to init: whatever started it is gone, so nothing
    is ever going to close it. Three of these reached 33 and 71 hours on this
    host and held roughly 247% CPU of two cores between them.
    """
    root = browsers_root or os.environ.get("CODEAGENT_BROWSERS", DEFAULT_BROWSERS_ROOT)
    try:
        clock = os.sysconf("SC_CLK_TCK")
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError):
        return []
    found: list[dict[str, Any]] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cmdline = _proc_field(pid, "cmdline").replace("\x00", " ").strip()
        if not cmdline.startswith(root):
            continue
        stat = _proc_field(pid, "stat")
        tail = stat.rpartition(")")[2].split()
        if len(tail) < 20:
            continue
        try:
            ppid = int(tail[1])
            started = float(tail[19]) / clock
        except (ValueError, ZeroDivisionError):
            continue
        if ppid != 1:
            continue  # still owned by a live parent
        age = uptime - started
        if age < min_age_s:
            continue
        found.append(
            {
                "pid": pid,
                "age_s": int(age),
                "command": cmdline[:120],
                "signalable": os.access(f"/proc/{pid}", os.W_OK),
            }
        )
    return sorted(found, key=lambda row: -row["age_s"])


def reap_orphan_browsers(**kwargs: Any) -> dict[str, Any]:
    """Kill the detached browsers we are allowed to signal; report the rest.

    A process owned by another user cannot be signalled from here, and saying
    so is the point: the CPU is still being burned, and someone with the
    privilege has to act. Silently reporting zero would be the worse answer.
    """
    killed: list[int] = []
    unreachable: list[dict[str, Any]] = []
    for row in find_orphan_browsers(**kwargs):
        try:
            os.kill(row["pid"], signal.SIGKILL)
            killed.append(row["pid"])
        except ProcessLookupError:
            continue
        except PermissionError:
            unreachable.append(row)
    emit_audit(
        {
            "event": "reap_orphan_browsers",
            "ok": True,
            "killed": len(killed),
            "unreachable": len(unreachable),
        }
    )
    return {"killed": killed, "unreachable": unreachable}


def run_startup_cleanup() -> dict[str, Any]:
    """Idempotent cleanup suitable for process start."""
    from codeagent_mcp.artifact_store.store import ArtifactStore

    arts = ArtifactStore()
    removed_arts = arts.cleanup_expired()
    spool = cleanup_spool()
    orphans = detect_orphans()

    # Deferred: the browser service imports the tool layer, which imports this.
    from codeagent_mcp.browser.service import get_browser_service
    from codeagent_mcp.exec.gate import get_exec_gate

    browser = get_browser_service().reap_if_stale()
    stale_execs = get_exec_gate().sweep()
    detached = reap_orphan_browsers()
    return {
        "artifacts_removed": removed_arts,
        "spool": spool,
        "orphans": orphans,
        "browser": browser,
        "stale_exec_gate_entries": stale_execs,
        "detached_browsers": detached,
    }
