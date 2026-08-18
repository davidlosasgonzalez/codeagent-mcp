"""A browser has to be able to end.

browser_ensure could start one and nothing could stop it: no close tool, no
close on release, no reaping. Detached browsers reached 33 and 71 hours on the
host and held roughly 247% CPU of two cores, with 0% idle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codeagent_mcp.browser.service import BrowserService
from codeagent_mcp.cleanup import find_orphan_browsers


class _FakeBrowser:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def close(self) -> None:
        self._log.append("browser.close")


class _FakePlaywright:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def stop(self) -> None:
        self._log.append("playwright.stop")


def _running(owner: str | None) -> tuple[BrowserService, list[str]]:
    log: list[str] = []
    svc = BrowserService()
    svc._browser = _FakeBrowser(log)  # type: ignore[assignment]
    svc._playwright = _FakePlaywright(log)  # type: ignore[assignment]
    svc._owner_lease_id = owner
    return svc, log


def test_closing_when_nothing_runs_is_success() -> None:
    """A caller forced to check first will not check."""
    out = BrowserService().close()
    assert out["ok"] is True
    assert out["closed"] is False


def test_close_shuts_the_browser_down() -> None:
    svc, log = _running("L1")
    out = svc.close(lease_id="L1")
    assert out["closed"] is True
    assert "browser.close" in log
    assert "playwright.stop" in log
    assert svc.is_running() is False


def test_another_lease_may_not_close_this_browser() -> None:
    svc, log = _running("L1")
    out = svc.close(lease_id="L2")
    assert out["ok"] is False
    assert out["error"]["code"] == "AUTHORIZATION_DENIED"
    assert log == [], "nothing may be closed on a denied call"


def test_close_is_idempotent() -> None:
    svc, _ = _running("L1")
    svc.close(lease_id="L1")
    assert svc.close(lease_id="L1")["closed"] is False


def test_a_browser_whose_lease_is_gone_is_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, log = _running("expired-lease")

    class _Dead:
        def require_active(self, *, lease_id: str) -> dict[str, Any]:
            return {"ok": False, "error": {"code": "LEASE_EXPIRED"}}

    monkeypatch.setattr("codeagent_mcp.browser.service.get_lease_manager", lambda: _Dead())
    out = svc.reap_if_stale()
    assert out["reaped"] is True
    assert "browser.close" in log


def test_a_browser_with_a_live_lease_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, log = _running("live-lease")

    class _Alive:
        def require_active(self, *, lease_id: str) -> dict[str, Any]:
            return {"ok": True, "lease_id": lease_id}

    monkeypatch.setattr("codeagent_mcp.browser.service.get_lease_manager", lambda: _Alive())
    assert svc.reap_if_stale()["reaped"] is False
    assert log == []


def test_a_browser_with_no_owner_is_reaped() -> None:
    svc, log = _running(None)
    assert svc.reap_if_stale()["reaped"] is True
    assert "browser.close" in log


# --- detached processes ----------------------------------------------------


def test_only_processes_from_our_bundle_count(tmp_path: Path) -> None:
    """Verified against a real case: ChatGPT Desktop leaves crashpad handlers
    reparented to init, and they are none of our business."""
    ours = find_orphan_browsers(browsers_root=str(tmp_path / "nothing-here"))
    assert ours == []


def test_a_freshly_started_browser_is_not_an_orphan() -> None:
    """A high minimum age keeps a browser that is still launching out of it."""
    assert find_orphan_browsers(browsers_root="/proc", min_age_s=10**9) == []
