"""An exec gate entry nobody can release is a lease that can never run again.

PROCESS_RUNNING came back three times for a lease with nothing running. The
gate was a bare set of ids: no owner, no clock, no way to tell a real in-flight
command from an entry whose thread died holding it.
"""

from __future__ import annotations

import threading

from codeagent_mcp.exec.gate import ExecLeaseGate


def test_one_exec_per_lease() -> None:
    gate = ExecLeaseGate()
    assert gate.try_enter("L1", command="pytest") is True
    assert gate.try_enter("L1", command="ruff") is False, "same lease must serialize"
    assert gate.try_enter("L2", command="ruff") is True, "a different lease is unaffected"


def test_exit_frees_the_lease() -> None:
    gate = ExecLeaseGate()
    gate.try_enter("L1")
    gate.exit("L1")
    assert gate.try_enter("L1") is True


def test_exiting_a_lease_that_never_entered_is_not_an_error() -> None:
    ExecLeaseGate().exit("never-seen")


def test_the_refusal_can_name_what_holds_it() -> None:
    """The old message could point at nothing, which is why it was unfixable."""
    gate = ExecLeaseGate()
    gate.try_enter("L1", command="pytest -q")
    holder = gate.holder("L1")
    assert holder is not None
    assert holder.command == "pytest -q"
    assert holder.age_s() >= 0


def test_an_entry_whose_thread_is_gone_is_reclaimed() -> None:
    """This is the leak: a worker stuck on a pipe never calls exit."""
    gate = ExecLeaseGate()
    thread = threading.Thread(target=lambda: gate.try_enter("L1", command="stuck"))
    thread.start()
    thread.join()
    assert gate.holder("L1") is not None, "the entry outlives the thread that made it"
    assert gate.try_enter("L1", command="next") is True, "a dead holder must not block"


def test_a_live_holder_is_not_reclaimed() -> None:
    """The reclaim must not defeat the serialization it protects."""
    gate = ExecLeaseGate()
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        gate.try_enter("L1", command="long build")
        entered.set()
        release.wait(timeout=5)

    thread = threading.Thread(target=hold)
    thread.start()
    entered.wait(timeout=5)
    try:
        assert gate.try_enter("L1", command="other") is False
    finally:
        release.set()
        thread.join(timeout=5)


def test_sweep_reports_what_it_dropped() -> None:
    gate = ExecLeaseGate()
    thread = threading.Thread(target=lambda: gate.try_enter("L1", command="stuck"))
    thread.start()
    thread.join()
    dropped = gate.sweep()
    assert [row["lease_id"] for row in dropped] == ["L1"]
    assert dropped[0]["command"] == "stuck"
    assert gate.sweep() == [], "sweeping twice must not invent work"


def test_status_shows_liveness_not_just_presence() -> None:
    gate = ExecLeaseGate()
    gate.try_enter("L1", command="pytest")
    status = gate.status()
    assert status["active"][0]["lease_id"] == "L1"
    assert status["active"][0]["thread_alive"] is True
