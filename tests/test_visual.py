"""Visual capture / compare — fixture HTTP server only."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from codeagent_mcp.artifact_store.store import ArtifactStore
from codeagent_mcp.browser.service import BrowserService, set_browser_service
from codeagent_mcp.tools.workspace import set_lease_manager
from codeagent_mcp.visual.service import VisualService, set_visual_service
from codeagent_mcp.workspace.lease_store import LeaseStore
from codeagent_mcp.workspace.leases import LeaseManager

HTML_RED = b"""<!doctype html><html><body style="margin:0;background:#ff0000">
<div id="box" style="width:100px;height:100px;background:#00ff00"></div>
</body></html>"""

HTML_BLUE = b"""<!doctype html><html><body style="margin:0;background:#0000ff">
<div id="box" style="width:100px;height:100px;background:#00ff00"></div>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    page = b"red"

    def do_GET(self):  # noqa: N802
        body = HTML_RED if self.page == b"red" else HTML_BLUE
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


@pytest.fixture()
def fixture_server():
    _Handler.page = b"red"
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/", _Handler
    server.shutdown()


@pytest.fixture()
def visual_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_server):
    url, handler = fixture_server
    monkeypatch.setenv("CODEAGENT_LEASE_STORE", str(tmp_path / "leases.json"))
    monkeypatch.setenv("CODEAGENT_LEASE_TTL_S", "2700")
    monkeypatch.setenv("CODEAGENT_BROWSER_PROFILE", str(tmp_path / "profile"))
    monkeypatch.setenv("CODEAGENT_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    mgr = LeaseManager(LeaseStore(tmp_path / "leases.json"), ttl_s=2700)
    set_lease_manager(mgr)
    browser = BrowserService()
    set_browser_service(browser)
    visual = VisualService(ArtifactStore(tmp_path / "artifacts"))
    set_visual_service(visual)
    yield visual, browser, mgr, url, handler
    browser.shutdown()
    set_browser_service(None)
    set_visual_service(None)
    set_lease_manager(None)


def _ready(browser, mgr, url):
    lid = mgr.acquire(project="demo")["lease_id"]
    assert browser.ensure(lease_id=lid)["ok"]
    assert browser.open(lease_id=lid, url=url)["ok"]
    return lid


def test_capture_viewport_and_get(visual_env):
    visual, browser, mgr, url, _ = visual_env
    lid = _ready(browser, mgr, url)
    meta, png = visual.capture(lease_id=lid, mode="viewport", device="desktop")
    assert meta["ok"], meta
    assert meta["artifact_id"]
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    got = visual.get(lease_id=lid, artifact_id=meta["artifact_id"])
    assert isinstance(got, tuple)
    gmeta, gpng = got
    assert gmeta["ok"] and gpng == png


def test_capture_element(visual_env):
    visual, browser, mgr, url, _ = visual_env
    lid = _ready(browser, mgr, url)
    meta, png = visual.capture(lease_id=lid, mode="element", selector="#box")
    assert meta["ok"], meta
    assert meta["width"] <= 200


def test_compare_identical_and_different(visual_env):
    visual, browser, mgr, url, handler = visual_env
    lid = _ready(browser, mgr, url)
    a_meta, _ = visual.capture(lease_id=lid, mode="viewport")
    b_meta, _ = visual.capture(lease_id=lid, mode="viewport")
    same = visual.compare(
        lease_id=lid,
        artifact_id_a=a_meta["artifact_id"],
        artifact_id_b=b_meta["artifact_id"],
    )
    assert isinstance(same, tuple)
    smeta, spng = same
    assert smeta["ok"] and smeta["identical"] is True
    assert spng[:8] == b"\x89PNG\r\n\x1a\n"

    handler.page = b"blue"
    assert browser.open(lease_id=lid, url=url)["ok"]
    c_meta, _ = visual.capture(lease_id=lid, mode="viewport")
    diff = visual.compare(
        lease_id=lid,
        artifact_id_a=a_meta["artifact_id"],
        artifact_id_b=c_meta["artifact_id"],
    )
    dmeta, _ = diff
    assert dmeta["ok"] and dmeta["identical"] is False
    assert dmeta["pixels_changed"] > 0


def test_expired_artifact(visual_env, monkeypatch):
    visual, browser, mgr, url, _ = visual_env
    lid = _ready(browser, mgr, url)
    monkeypatch.setattr(visual.store, "ttl_s", 0)
    meta, _ = visual.capture(lease_id=lid)
    # force expire
    data = visual.store._load_index()
    aid = meta["artifact_id"]
    data["artifacts"][aid]["expires_at"] = 1
    visual.store._save_index(data)
    missing = visual.get(lease_id=lid, artifact_id=aid)
    assert missing["ok"] is False and missing["error"]["code"] == "NOT_FOUND"


def test_tool_result_wrapper():
    from fastmcp.tools.base import ToolResult

    from codeagent_mcp.tools.visual import _as_tool_result

    err = {"ok": False, "error": {"code": "X"}}
    assert _as_tool_result(err) is err
    meta = {"ok": True, "artifact_id": "abc"}
    out = _as_tool_result((meta, b"\x89PNG\r\n\x1a\nxxxx"))
    assert isinstance(out, ToolResult)
    assert out.structured_content is not None
    assert out.structured_content["artifact_id"] == "abc"
