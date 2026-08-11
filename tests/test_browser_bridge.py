"""Browser bridge — fixture HTTP server only."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from codeagent_mcp.browser.service import BrowserService, set_browser_service
from codeagent_mcp.browser.urls import validate_navigation_url
from codeagent_mcp.tools.workspace import set_lease_manager
from codeagent_mcp.workspace.lease_store import LeaseStore
from codeagent_mcp.workspace.leases import LeaseManager

HTML = b"""<!doctype html>
<html><head><title>Fixture</title></head>
<body>
  <h1>Hello Fixture</h1>
  <input id="name" name="name" placeholder="Your name" />
  <button id="go" type="button">Go</button>
  <p id="out"></p>
  <script>
    document.getElementById('go').onclick = () => {
      const v = document.getElementById('name').value;
      document.getElementById('out').textContent = 'Hi ' + v;
      console.log('clicked:' + v);
    };
  </script>
</body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(HTML)))
        self.end_headers()
        self.wfile.write(HTML)

    def log_message(self, format, *args):  # noqa: A003
        return


@pytest.fixture()
def fixture_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/"
    server.shutdown()


@pytest.fixture()
def browser_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_server: str):
    monkeypatch.setenv("CODEAGENT_LEASE_STORE", str(tmp_path / "leases.json"))
    monkeypatch.setenv("CODEAGENT_LEASE_TTL_S", "2700")
    monkeypatch.setenv("CODEAGENT_BROWSER_PROFILE", str(tmp_path / "profile"))
    from conftest import override_projects

    from codeagent_mcp.workspace import projects as projects_mod

    root = tmp_path / "proj"
    root.mkdir()
    override_projects(
        monkeypatch,
        {"demo": projects_mod.ProjectConfig(name="demo", root=str(root))},
    )
    mgr = LeaseManager(LeaseStore(tmp_path / "leases.json"), ttl_s=2700)
    set_lease_manager(mgr)
    svc = BrowserService()
    set_browser_service(svc)
    yield svc, mgr, fixture_server
    svc.shutdown()
    set_browser_service(None)
    set_lease_manager(None)


def _lease(mgr: LeaseManager) -> str:
    acq = mgr.acquire(project="demo")
    assert acq["ok"]
    return acq["lease_id"]


def test_url_allowlist_unit() -> None:
    assert validate_navigation_url("http://127.0.0.1:9/")
    with pytest.raises(ValueError):
        validate_navigation_url("https://example.com/")
    with pytest.raises(ValueError):
        validate_navigation_url("file:///etc/passwd")


def test_ensure_open_action_snapshot(browser_env) -> None:
    svc, mgr, url = browser_env
    lid = _lease(mgr)
    ens = svc.ensure(lease_id=lid)
    assert ens["ok"] and ens["status"] == "ready"
    opened = svc.open(lease_id=lid, url=url)
    assert opened["ok"], opened
    assert "127.0.0.1" in opened["url"]
    assert svc.action(lease_id=lid, action="fill", selector="#name", value="Ada")["ok"]
    assert svc.action(lease_id=lid, action="click", selector="#go")["ok"]
    snap = svc.snapshot(lease_id=lid)
    assert snap["ok"], snap
    assert "Hello Fixture" in str(snap.get("dom"))
    # no image fields
    assert "image" not in snap and "png" not in snap


def test_block_external_url(browser_env) -> None:
    svc, mgr, _ = browser_env
    lid = _lease(mgr)
    assert svc.ensure(lease_id=lid)["ok"]
    blocked = svc.open(lease_id=lid, url="https://example.com/")
    assert blocked["ok"] is False
    assert blocked["error"]["code"] == "RISK_BLOCKED"


def test_block_file_url(browser_env) -> None:
    svc, mgr, _ = browser_env
    lid = _lease(mgr)
    assert svc.ensure(lease_id=lid)["ok"]
    blocked = svc.open(lease_id=lid, url="file:///etc/passwd")
    assert blocked["error"]["code"] == "RISK_BLOCKED"


def test_lease_required(browser_env) -> None:
    svc, _, _ = browser_env
    out = svc.ensure(lease_id="")
    assert out["error"]["code"] == "LEASE_REQUIRED"


def test_set_viewport_and_reload(browser_env) -> None:
    svc, mgr, url = browser_env
    lid = _lease(mgr)
    ens = svc.ensure(lease_id=lid, width=820, height=1180)
    assert ens["ok"], ens
    assert ens["viewport"]["width"] == 820
    assert ens["viewport"]["height"] == 1180
    moved = svc.set_viewport(lease_id=lid, width=390, height=844)
    assert moved["ok"], moved
    assert moved["viewport"]["width"] == 390
    assert svc.open(lease_id=lid, url=url)["ok"]
    reloaded = svc.reload(lease_id=lid, ignore_cache=True)
    assert reloaded["ok"], reloaded
    assert "127.0.0.1" in reloaded["url"]
    assert reloaded["ignore_cache"] is True
    bad = svc.set_viewport(lease_id=lid, width=0, height=100)
    assert bad["ok"] is False
    assert bad["error"]["code"] == "INVALID_ARGUMENT"
