"""Serialize one active exec_run per lease_id.

The gate used to be a bare set of lease ids. That is enough to serialize and
not enough to diagnose: when an entry leaked, every later call answered
PROCESS_RUNNING and could name neither what held it nor for how long.

Two changes. Each entry now records its holder, so the refusal can say what it
is waiting for. And an entry whose holding thread no longer exists is stale by
definition and is reclaimed — a leak becomes a logged event instead of a lease
that can never run a command again.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExecHolder:
    """Who entered the gate, and when."""

    thread_ident: int
    command: str
    started_at: float

    def age_s(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)


def _thread_alive(ident: int) -> bool:
    return any(t.ident == ident for t in threading.enumerate())


class ExecLeaseGate:
    """At most one in-flight exec per lease (Challenger / Gate F)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, ExecHolder] = {}
        self._reclaimed = 0

    def try_enter(self, lease_id: str, *, command: str = "") -> bool:
        with self._lock:
            holder = self._active.get(lease_id)
            if holder is not None:
                if _thread_alive(holder.thread_ident):
                    return False
                # The thread that entered is gone; nothing will ever call exit.
                self._reclaimed += 1
            current = threading.current_thread()
            self._active[lease_id] = ExecHolder(
                thread_ident=current.ident or 0,
                command=command,
                started_at=time.monotonic(),
            )
            return True

    def exit(self, lease_id: str) -> None:
        with self._lock:
            self._active.pop(lease_id, None)

    def holder(self, lease_id: str) -> ExecHolder | None:
        with self._lock:
            return self._active.get(lease_id)

    def sweep(self) -> list[dict[str, Any]]:
        """Drop entries whose holding thread is gone; report what was dropped."""
        dropped: list[dict[str, Any]] = []
        with self._lock:
            for lease_id, holder in list(self._active.items()):
                if _thread_alive(holder.thread_ident):
                    continue
                del self._active[lease_id]
                self._reclaimed += 1
                dropped.append(
                    {
                        "lease_id": lease_id,
                        "command": holder.command,
                        "age_s": round(holder.age_s(), 1),
                    }
                )
        return dropped

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": [
                    {
                        "lease_id": lease_id,
                        "command": holder.command,
                        "age_s": round(holder.age_s(), 1),
                        "thread_alive": _thread_alive(holder.thread_ident),
                    }
                    for lease_id, holder in self._active.items()
                ],
                "reclaimed_total": self._reclaimed,
            }


_GATE = ExecLeaseGate()


def get_exec_gate() -> ExecLeaseGate:
    return _GATE


def set_exec_gate(gate: ExecLeaseGate | None) -> None:
    """Test hook."""
    global _GATE
    _GATE = gate if gate is not None else ExecLeaseGate()
