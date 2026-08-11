"""Private TMPDIR pin and non-overridable via exec_run."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from codeagent_mcp.exec.env import CODEAGENT_DEFAULT_TMPDIR, merge_env_overrides


def test_default_tmpdir_is_service_private() -> None:
    assert CODEAGENT_DEFAULT_TMPDIR == Path("/var/lib/codeagent-mcp/tmp")


def test_merge_env_pins_tmpdir(tmp_path, monkeypatch) -> None:
    private_tmp = tmp_path / "svc-tmp"
    monkeypatch.setenv("TMPDIR", str(private_tmp))
    env = merge_env_overrides(None)
    assert isinstance(env, dict) and env.get("ok") is not False
    assert env["TMPDIR"] == str(private_tmp)
    assert env["TEMP"] == env["TMPDIR"]
    assert env["TMP"] == env["TMPDIR"]
    assert Path(env["TMPDIR"]).is_dir()


def test_tmpdir_override_blocked() -> None:
    err = cast("dict[str, Any]", merge_env_overrides({"TMPDIR": "/tmp/evil"}))
    assert err["ok"] is False
    assert err["error"]["code"] == "RISK_BLOCKED"
