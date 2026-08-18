"""service_logs and http_check: argument confinement and what never comes back.

Both tools widen what a caller can reach, so both are pinned by what they refuse:
service_logs carries caller text to a privileged helper for the first time, and
http_check makes the server fetch a URL and report on it. The interesting
assertions here are the negative ones.
"""

from __future__ import annotations

import asyncio
import http.server
import socket
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from codeagent_mcp.server import create_server
from codeagent_mcp.service_ctl import ServiceCtlError, call_service_ctl


def _registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: dict) -> None:
    path = tmp_path / "projects.yaml"
    path.write_text(yaml.safe_dump({"projects": [entry]}), encoding="utf-8")
    monkeypatch.setenv("CODEAGENT_PROJECTS_FILE", str(path))


def _call(tool_name: str, **kwargs: Any) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        server = create_server(transport="stdio")
        result = await server.call_tool(tool_name, kwargs)
        assert result.structured_content is not None
        return result.structured_content

    return asyncio.run(_run())


# --- the wire: what the helper is allowed to receive ------------------------


@pytest.fixture()
def fake_ctl(tmp_path: Path):
    """A stand-in helper that records the whole line it was sent."""
    sock_path = tmp_path / "ctl.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(2)
    received: list[str] = []

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                line = conn.recv(256).decode().strip()
                received.append(line)
                conn.sendall(b"=== app.service : last 2 lines ===\nhello\n")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield {"path": str(sock_path), "received": received}
    server.close()
    thread.join(timeout=2)


def test_logs_verb_carries_its_tokens(fake_ctl) -> None:
    out = call_service_ctl(fake_ctl["path"], "LOGS", args=["app.service", "20"], timeout_s=5)
    assert fake_ctl["received"] == ["LOGS app.service 20"]
    assert "hello" in out


@pytest.mark.parametrize(
    "arg",
    [
        "a b",
        "app.service; rm -rf /",
        "app\nRESTART sshd",
        "../../etc/passwd",
        "$(id)",
        "x" * 65,
        "",
    ],
)
def test_dangerous_tokens_never_reach_the_socket(fake_ctl, arg: str) -> None:
    with pytest.raises(ServiceCtlError, match="refusing argument"):
        call_service_ctl(fake_ctl["path"], "LOGS", args=[arg], timeout_s=5)
    assert fake_ctl["received"] == []


def test_argument_count_is_bounded(fake_ctl) -> None:
    with pytest.raises(ServiceCtlError, match="at most"):
        call_service_ctl(fake_ctl["path"], "LOGS", args=["a", "1", "extra"], timeout_s=5)
    assert fake_ctl["received"] == []


def test_snapshot_is_a_known_verb(fake_ctl) -> None:
    call_service_ctl(fake_ctl["path"], "SNAPSHOT", timeout_s=5)
    assert fake_ctl["received"] == ["SNAPSHOT"]


# --- service_logs ----------------------------------------------------------


def test_service_logs_sends_only_a_line_count_when_no_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_ctl
) -> None:
    _registry(
        tmp_path,
        monkeypatch,
        {"id": "app", "root": "/srv/app", "control_socket": fake_ctl["path"]},
    )
    out = _call("service_logs", project="app", lines=5)
    assert out["ok"] is True
    assert out["unit"] == "primary"
    assert fake_ctl["received"] == ["LOGS 5"]


def test_service_logs_caps_the_line_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_ctl
) -> None:
    _registry(
        tmp_path,
        monkeypatch,
        {"id": "app", "root": "/srv/app", "control_socket": fake_ctl["path"]},
    )
    out = _call("service_logs", project="app", lines=999_999)
    assert out["ok"] is True
    assert out["lines"] == 300
    assert fake_ctl["received"] == ["LOGS 300"]


def test_service_logs_rejects_a_shell_shaped_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_ctl
) -> None:
    _registry(
        tmp_path,
        monkeypatch,
        {"id": "app", "root": "/srv/app", "control_socket": fake_ctl["path"]},
    )
    out = _call("service_logs", project="app", unit="a b; rm -rf /")
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_ARGUMENT"
    assert fake_ctl["received"] == [], "nothing may reach the helper"


def test_service_logs_surfaces_an_undeclared_unit_as_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper answers ERR for a unit it was not told about; that is not output."""
    sock_path = tmp_path / "err.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)

    def serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            conn.recv(256)
            conn.sendall(b"ERR unit_not_declared\n")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        _registry(
            tmp_path,
            monkeypatch,
            {"id": "app", "root": "/srv/app", "control_socket": str(sock_path)},
        )
        out = _call("service_logs", project="app", unit="sshd")
        assert out["ok"] is False
        assert "unit_not_declared" in out["error"]["message"]
    finally:
        server.close()
        thread.join(timeout=2)


# --- http_check ------------------------------------------------------------


@pytest.fixture()
def loopback_app(tmp_path: Path):
    """A tiny loopback server: / is fine, /token answers with a credential."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            if self.path == "/token":
                body = b'{"token":"SUPER-SECRET-VALUE"}'
                self.send_response(200)
            elif self.path == "/":
                body = b"<html>ok</html>"
                self.send_response(200)
            else:
                body = b"nope"
                self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _app_registry(tmp_path, monkeypatch, base: str) -> None:
    _registry(
        tmp_path,
        monkeypatch,
        {
            "id": "app",
            "root": "/srv/app",
            "control_socket": "/run/app-ctl.sock",
            "preview_url": base,
        },
    )


def test_http_check_reports_a_live_page(tmp_path, monkeypatch, loopback_app) -> None:
    _app_registry(tmp_path, monkeypatch, loopback_app)
    out = _call("http_check", project="app", path="/")
    assert out["ok"] is True
    assert out["http_status"] == 200
    assert out["ok_status"] is True
    assert out["bytes_read"] == len("<html>ok</html>")


def test_http_check_never_returns_the_body(tmp_path, monkeypatch, loopback_app) -> None:
    """A 200 can be a credential: /token answers fine and must stay unquoted."""
    _app_registry(tmp_path, monkeypatch, loopback_app)
    out = _call("http_check", project="app", path="/token")
    assert out["ok"] is True
    assert out["http_status"] == 200
    assert "SUPER-SECRET-VALUE" not in str(out)
    assert "body" not in out


def test_http_check_reports_a_missing_path_without_failing(
    tmp_path, monkeypatch, loopback_app
) -> None:
    _app_registry(tmp_path, monkeypatch, loopback_app)
    out = _call("http_check", project="app", path="/health")
    assert out["ok"] is True
    assert out["http_status"] == 404
    assert out["ok_status"] is False


@pytest.mark.parametrize("path", ["../etc/passwd", "//evil.example.com", "relative"])
def test_http_check_refuses_paths_that_could_leave_the_host(
    tmp_path, monkeypatch, loopback_app, path: str
) -> None:
    _app_registry(tmp_path, monkeypatch, loopback_app)
    out = _call("http_check", project="app", path=path)
    assert out["ok"] is False
    assert out["error"]["code"] in {"INVALID_ARGUMENT", "RISK_BLOCKED"}


def test_http_check_needs_a_declared_base_url(tmp_path, monkeypatch) -> None:
    _registry(
        tmp_path,
        monkeypatch,
        {"id": "app", "root": "/srv/app", "control_socket": "/run/app-ctl.sock"},
    )
    out = _call("http_check", project="app", path="/")
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_ARGUMENT"


def test_preview_url_must_be_loopback(tmp_path, monkeypatch) -> None:
    """Same rule as health_url: the server fetches it, so it cannot leave the host."""
    from codeagent_mcp.workspace import projects as projects_mod

    _registry(
        tmp_path,
        monkeypatch,
        {
            "id": "app",
            "root": "/srv/app",
            "preview_url": "http://169.254.169.254/latest/meta-data/",
        },
    )
    with pytest.raises(ValueError, match="loopback"):
        projects_mod.get_project("app")
