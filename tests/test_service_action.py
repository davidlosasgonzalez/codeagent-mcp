"""service_action, and telling "it restarted" apart from "it is answering".

service_action exists because a client can reasonably refuse to send
exec_run(run_as=...) — there the caller picks the command. Here it does not: the
command lives on the host and the caller only names it. So the tests are about
the name never becoming a command.

health_ready exists because restart_ok on a worker with no HTTP surface said
"done" while the aggregated endpoint still answered 503.
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


def _call(tool_name: str, **kwargs: Any) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        server = create_server(transport="stdio")
        result = await server.call_tool(tool_name, kwargs)
        assert result.structured_content is not None
        return result.structured_content

    return asyncio.run(_run())


@pytest.fixture()
def ctl(tmp_path: Path):
    """A stand-in helper that records the line it was sent and answers a menu."""
    sock_path = tmp_path / "ctl.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(4)
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
                parts = line.split()
                if parts[0] != "ACTION":
                    conn.sendall(b"ActiveState=active\n")
                elif len(parts) == 1:
                    conn.sendall(b"health_deep\nhealth_shallow\n")
                elif parts[1] == "health_deep":
                    conn.sendall(b"OK calendar\n__codeagent_exit__=0\n")
                else:
                    conn.sendall(b"ERR action_not_declared\n")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield {"path": str(sock_path), "received": received}
    server.close()
    thread.join(timeout=2)


def _registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **extra: Any) -> None:
    path = tmp_path / "projects.yaml"
    entry = {"id": "app", "root": str(tmp_path), **extra}
    path.write_text(yaml.safe_dump({"projects": [entry]}), encoding="utf-8")
    monkeypatch.setenv("CODEAGENT_PROJECTS_FILE", str(path))


def test_listing_asks_for_the_menu(tmp_path, monkeypatch, ctl) -> None:
    _registry(tmp_path, monkeypatch, control_socket=ctl["path"])
    out = _call("service_action", project="app")
    assert out["ok"] is True
    assert out["actions"] == ["health_deep", "health_shallow"]
    assert ctl["received"] == ["ACTION"]


def test_a_declared_action_runs_and_reports_its_exit_code(tmp_path, monkeypatch, ctl) -> None:
    _registry(tmp_path, monkeypatch, control_socket=ctl["path"])
    out = _call("service_action", project="app", action="health_deep")
    assert out["ok"] is True
    assert out["exit_code"] == 0
    assert out["output"] == "OK calendar"
    assert ctl["received"] == ["ACTION health_deep"]


def test_an_undeclared_action_is_an_error_not_output(tmp_path, monkeypatch, ctl) -> None:
    _registry(tmp_path, monkeypatch, control_socket=ctl["path"])
    out = _call("service_action", project="app", action="whatever")
    assert out["ok"] is False
    assert "action_not_declared" in out["error"]["message"]


@pytest.mark.parametrize(
    "name",
    [
        "health deep",
        "health;id",
        "../../bin/sh",
        "HEALTH_DEEP",
        "$(id)",
        "health\nACTION other",
        "x" * 40,
    ],
)
def test_a_name_never_becomes_a_command(tmp_path, monkeypatch, ctl, name: str) -> None:
    _registry(tmp_path, monkeypatch, control_socket=ctl["path"])
    out = _call("service_action", project="app", action=name)
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_ARGUMENT"
    assert ctl["received"] == [], "nothing may reach the helper"


def test_a_project_without_a_socket_has_no_actions(tmp_path, monkeypatch, ctl) -> None:
    _registry(tmp_path, monkeypatch, control_socket=ctl["path"])
    out = _call("service_action", project="nope")
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_ARGUMENT"


# --- restarted is not the same as answering --------------------------------


@pytest.fixture()
def flaky_health(tmp_path: Path):
    """Answers 503 for the first few probes, then 200 — like a worker registering."""
    state = {"probes": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            state["probes"] += 1
            code = 200 if state["probes"] > 2 else 503
            body = b'{"ok":true}' if code == 200 else b'{"ok":false}'
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"url": f"http://127.0.0.1:{server.server_port}/health", "state": state}
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_no_health_endpoint_means_unknown_not_failed(tmp_path, monkeypatch, ctl) -> None:
    """None is not False: a project with no check has not failed one."""
    _registry(tmp_path, monkeypatch, control_socket=ctl["path"])
    out = _call("service_status", project="app")
    assert out["ok"] is True
    assert out["health_ready"] is None


def test_health_ready_is_false_while_the_endpoint_is_still_503(
    tmp_path, monkeypatch, ctl, flaky_health
) -> None:
    _registry(tmp_path, monkeypatch, control_socket=ctl["path"], health_url=flaky_health["url"])
    out = _call("service_status", project="app")
    assert out["ok"] is True
    assert out["health_ready"] is False
    assert out["health"]["http_status"] == 503


def test_health_ready_is_true_once_it_answers(tmp_path, monkeypatch, ctl, flaky_health) -> None:
    flaky_health["state"]["probes"] = 99  # already past the warm-up
    _registry(tmp_path, monkeypatch, control_socket=ctl["path"], health_url=flaky_health["url"])
    out = _call("service_status", project="app")
    assert out["health_ready"] is True
    assert out["health"]["http_status"] == 200
