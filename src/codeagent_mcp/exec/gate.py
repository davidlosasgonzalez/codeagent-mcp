"""Serialize one active exec_run per lease_id."""

from __future__ import annotations

import threading


class ExecLeaseGate:
    """At most one in-flight exec per lease (Challenger / Gate F)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: set[str] = set()

    def try_enter(self, lease_id: str) -> bool:
        with self._lock:
            if lease_id in self._active:
                return False
            self._active.add(lease_id)
            return True

    def exit(self, lease_id: str) -> None:
        with self._lock:
            self._active.discard(lease_id)


_GATE = ExecLeaseGate()


def get_exec_gate() -> ExecLeaseGate:
    return _GATE


def set_exec_gate(gate: ExecLeaseGate | None) -> None:
    """Test hook."""
    global _GATE
    _GATE = gate if gate is not None else ExecLeaseGate()
