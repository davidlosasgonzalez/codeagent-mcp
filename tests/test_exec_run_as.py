"""exec_run(run_as=...): which account, and everything that must be refused.

This is the only path in the server that crosses a uid boundary, so the tests
are weighted towards what it declines to do. The crossing itself is exercised
against a stand-in helper — a real one would need a second account, and the
privileged half is verified on the host instead.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from codeagent_mcp.exec.runas import discard, split_exit_code, spool_root, write_spec
from codeagent_mcp.server import create_server
from codeagent_mcp.tools.workspace import set_lease_manager
from codeagent_mcp.workspace.lease_store import LeaseStore
from codeagent_mcp.workspace.leases import LeaseManager

# --- registry: which account may be named -----------------------------------


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: dict):
    from codeagent_mcp.workspace import projects as projects_mod

    path = tmp_path / "projects.yaml"
    path.write_text(yaml.safe_dump({"projects": [entry]}), encoding="utf-8")
    monkeypatch.setenv("CODEAGENT_PROJECTS_FILE", str(path))
    return projects_mod.get_project(entry["id"])


def test_run_as_user_is_optional(tmp_path, monkeypatch) -> None:
    cfg = _load(tmp_path, monkeypatch, {"id": "app", "root": "/srv/app"})
    assert cfg is not None
    assert cfg.run_as_user is None


def test_run_as_user_round_trips(tmp_path, monkeypatch) -> None:
    cfg = _load(tmp_path, monkeypatch, {"id": "app", "root": "/srv/app", "run_as_user": "appsvc"})
    assert cfg is not None
    assert cfg.run_as_user == "appsvc"


def test_root_is_refused_by_name(tmp_path, monkeypatch) -> None:
    """The whole point of the field is reaching a service account, not root."""
    with pytest.raises(ValueError, match="may not be root"):
        _load(tmp_path, monkeypatch, {"id": "app", "root": "/srv/app", "run_as_user": "root"})


@pytest.mark.parametrize(
    "name", ["0", "app svc", "app;id", "../root", "UPPER", "a" * 40, "-leading"]
)
def test_malformed_account_names_are_refused(tmp_path, monkeypatch, name: str) -> None:
    with pytest.raises(ValueError, match="plain account name"):
        _load(tmp_path, monkeypatch, {"id": "app", "root": "/srv/app", "run_as_user": name})


# --- the spool: how the command travels -------------------------------------


def test_spec_is_written_private_and_complete(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEAGENT_RUNAS_SPOOL", str(tmp_path / "runas"))
    token = write_spec(argv=["echo", "hola mundo"], cwd="/srv/app", timeout_s=30)
    path = spool_root() / f"{token}.json"
    spec = json.loads(path.read_text())
    assert spec["argv"] == ["echo", "hola mundo"], "an argument with a space must survive"
    assert spec["cwd"] == "/srv/app"
    assert spec["timeout_s"] == 30
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_tokens_do_not_repeat(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEAGENT_RUNAS_SPOOL", str(tmp_path / "runas"))
    tokens = {write_spec(argv=["true"], cwd="/srv", timeout_s=5) for _ in range(20)}
    assert len(tokens) == 20


def test_discard_removes_an_unconsumed_spec(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEAGENT_RUNAS_SPOOL", str(tmp_path / "runas"))
    token = write_spec(argv=["true"], cwd="/srv", timeout_s=5)
    discard(token)
    assert not (spool_root() / f"{token}.json").exists()
    discard(token)  # idempotent


# --- the trailer: success must be reported, not inferred --------------------


def test_exit_code_is_read_from_the_trailer() -> None:
    body, code = split_exit_code("linea uno\nlinea dos\n\n__codeagent_exit__=7\n")
    assert code == 7
    assert body == "linea uno\nlinea dos"


def test_missing_trailer_yields_none_not_zero() -> None:
    """A helper that died mid-way must not read as success."""
    body, code = split_exit_code("salida a medias\n")
    assert code is None
    assert body == "salida a medias\n"


def test_output_that_mentions_the_marker_does_not_confuse_it() -> None:
    body, code = split_exit_code("echo __codeagent_exit__=1\n__codeagent_exit__=0\n")
    assert code == 0
    assert "echo __codeagent_exit__=1" in body


# --- the tool: refusals -----------------------------------------------------


@pytest.fixture()
def leased(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A writable project with a lease, plus a stand-in privileged helper."""
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
                received.append(conn.recv(256).decode().strip())
                conn.sendall(b"soy appuser\n__codeagent_exit__=0\n")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setenv("CODEAGENT_RUNAS_SPOOL", str(tmp_path / "runas"))
    monkeypatch.setenv("CODEAGENT_LEASE_STORE", str(tmp_path / "leases.json"))

    def registry(**extra: Any) -> None:
        entry = {"id": "app", "root": str(root), "writable": True, **extra}
        path = tmp_path / "projects.yaml"
        path.write_text(yaml.safe_dump({"projects": [entry]}), encoding="utf-8")
        monkeypatch.setenv("CODEAGENT_PROJECTS_FILE", str(path))

    registry(control_socket=str(sock_path), run_as_user="appuser")
    mgr = LeaseManager(LeaseStore(tmp_path / "leases.json"), ttl_s=2700)
    set_lease_manager(mgr)
    lease = mgr.acquire(project="app")["lease_id"]

    yield {"lease": lease, "received": received, "registry": registry, "root": str(root)}

    set_lease_manager(None)
    server.close()
    thread.join(timeout=2)


def _exec(**kwargs: Any) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        server = create_server(transport="stdio")
        result = await server.call_tool("exec_run", kwargs)
        assert result.structured_content is not None
        return result.structured_content

    return asyncio.run(_run())


@pytest.mark.parametrize("who", ["root", "otherapp", "nobody", "appuser2"])
def test_only_the_declared_account_is_accepted(leased, who: str) -> None:
    out = _exec(lease_id=leased["lease"], command=["id"], run_as=who)
    assert out["ok"] is False
    assert out["error"]["code"] == "RISK_BLOCKED"
    assert leased["received"] == [], "nothing may reach the helper"


def test_run_as_needs_the_project_to_declare_an_account(leased, tmp_path) -> None:
    leased["registry"](control_socket="/run/app.sock")  # no run_as_user
    out = _exec(lease_id=leased["lease"], command=["id"], run_as="appuser")
    assert out["ok"] is False
    assert out["error"]["code"] == "RISK_BLOCKED"


def test_run_as_needs_a_control_socket(leased) -> None:
    leased["registry"](run_as_user="appuser")  # no control_socket
    out = _exec(lease_id=leased["lease"], command=["id"], run_as="appuser")
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_ARGUMENT"


def test_cwd_still_cannot_leave_the_project(leased) -> None:
    out = _exec(lease_id=leased["lease"], command=["pwd"], cwd="/etc", run_as="appuser")
    assert out["ok"] is False
    assert out["error"]["code"] == "PATH_OUTSIDE_ROOT"
    assert leased["received"] == []


def test_no_lease_no_crossing(leased) -> None:
    out = _exec(lease_id="", command=["id"], run_as="appuser")
    assert out["ok"] is False
    assert out["error"]["code"] == "LEASE_REQUIRED"
    assert leased["received"] == []


# --- the tool: the accepted path -------------------------------------------


def test_accepted_call_sends_only_a_token(leased) -> None:
    out = _exec(lease_id=leased["lease"], command=["id", "-un"], run_as="appuser")
    assert out["ok"] is True
    assert out["ran_as"] == "appuser"
    assert out["exit_code"] == 0
    assert out["output"] == "soy appuser"
    assert out["stdout_stderr_merged"] is True

    assert len(leased["received"]) == 1
    verb, token = leased["received"][0].split()
    assert verb == "RUNAS"
    # The command itself never appears on the wire.
    assert "id" not in leased["received"][0].split()[1:] or token != "id"
    assert len(token) >= 16
