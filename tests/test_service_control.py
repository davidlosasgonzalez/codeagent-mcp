"""Service control: registry validation, socket protocol and tool surface."""

from __future__ import annotations

import asyncio
import socket
import threading
from pathlib import Path

import pytest
import yaml

from codeagent_mcp.server import create_server
from codeagent_mcp.service_ctl import ServiceCtlError, call_service_ctl
from codeagent_mcp.workspace import projects as projects_mod


def _write_registry(tmp_path: Path, entry: dict) -> Path:
    path = tmp_path / "projects.yaml"
    path.write_text(yaml.safe_dump({"projects": [entry]}), encoding="utf-8")
    return path


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: dict):
    path = _write_registry(tmp_path, entry)
    monkeypatch.setenv("CODEAGENT_PROJECTS_FILE", str(path))
    return projects_mod.get_project(entry["id"])


# --- registry contract -----------------------------------------------------


def test_control_socket_and_health_url_are_optional(tmp_path, monkeypatch) -> None:
    cfg = _load(tmp_path, monkeypatch, {"id": "app", "root": "/srv/app"})
    assert cfg is not None
    assert cfg.control_socket is None
    assert cfg.health_url is None


def test_control_socket_must_be_absolute(tmp_path, monkeypatch) -> None:
    with pytest.raises(ValueError, match="absolute path"):
        _load(
            tmp_path,
            monkeypatch,
            {"id": "app", "root": "/srv/app", "control_socket": "run/app.sock"},
        )


def test_health_url_must_be_loopback(tmp_path, monkeypatch) -> None:
    """A non-loopback probe would make the registry a request forwarder."""
    with pytest.raises(ValueError, match="loopback"):
        _load(
            tmp_path,
            monkeypatch,
            {
                "id": "app",
                "root": "/srv/app",
                "control_socket": "/run/app.sock",
                "health_url": "http://169.254.169.254/latest/meta-data/",
            },
        )


def test_health_url_must_be_http(tmp_path, monkeypatch) -> None:
    with pytest.raises(ValueError, match="http or https"):
        _load(
            tmp_path,
            monkeypatch,
            {
                "id": "app",
                "root": "/srv/app",
                "control_socket": "/run/app.sock",
                "health_url": "file:///etc/shadow",
            },
        )


def test_valid_entry_round_trips(tmp_path, monkeypatch) -> None:
    cfg = _load(
        tmp_path,
        monkeypatch,
        {
            "id": "app",
            "root": "/srv/app",
            "control_socket": "/run/app-ctl.sock",
            "health_url": "http://127.0.0.1:9000/health",
        },
    )
    assert cfg is not None
    assert cfg.control_socket == "/run/app-ctl.sock"
    assert cfg.health_url == "http://127.0.0.1:9000/health"


# --- socket protocol -------------------------------------------------------


@pytest.fixture()
def fake_ctl(tmp_path):
    """A stand-in helper that echoes the verb it received."""
    sock_path = tmp_path / "ctl.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    received: list[str] = []

    def serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            verb = conn.recv(64).decode().strip()
            received.append(verb)
            conn.sendall(f"ActiveState=active\nverb={verb}\npassword: hunter2\n".encode())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield {"path": str(sock_path), "received": received}
    server.close()
    thread.join(timeout=2)


def test_call_service_ctl_sends_the_verb(fake_ctl) -> None:
    out = call_service_ctl(fake_ctl["path"], "RESTART", timeout_s=5)
    assert fake_ctl["received"] == ["RESTART"]
    assert "ActiveState=active" in out


def test_reply_is_redacted(fake_ctl) -> None:
    """Journal lines reach the client, so credentials must not survive."""
    out = call_service_ctl(fake_ctl["path"], "STATUS", timeout_s=5)
    assert "hunter2" not in out
    assert "<redacted>" in out


def test_unknown_verb_never_reaches_the_socket(fake_ctl) -> None:
    with pytest.raises(ServiceCtlError, match="unsupported verb"):
        call_service_ctl(fake_ctl["path"], "STOP")  # type: ignore[arg-type]
    assert fake_ctl["received"] == []


def test_missing_socket_is_reported(tmp_path) -> None:
    with pytest.raises(ServiceCtlError, match="control socket missing"):
        call_service_ctl(str(tmp_path / "absent.sock"), "STATUS", timeout_s=2)


# --- tool surface ----------------------------------------------------------


def _tool_names(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    server = create_server(transport="stdio")

    async def _names() -> list[str]:
        return sorted(t.name for t in await server.list_tools())

    return asyncio.run(_names())


def test_tools_absent_without_a_control_socket(monkeypatch) -> None:
    """The default registry declares none, so the tools must not appear."""
    names = _tool_names(monkeypatch)
    assert "service_restart" not in names
    assert "service_status" not in names


def test_tools_registered_when_a_project_declares_one(tmp_path, monkeypatch) -> None:
    path = _write_registry(
        tmp_path,
        {"id": "app", "root": "/srv/app", "control_socket": "/run/app-ctl.sock"},
    )
    monkeypatch.setenv("CODEAGENT_PROJECTS_FILE", str(path))
    names = _tool_names(monkeypatch)
    assert {"service_status", "service_restart", "service_start"} <= set(names)
