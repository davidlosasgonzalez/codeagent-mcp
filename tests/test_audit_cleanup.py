"""Audit / cleanup / rate-limit unit tests."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from codeagent_mcp.audit import RedactingFilter, classify_failure, emit_audit, redact
from codeagent_mcp.cleanup import cleanup_spool, detect_orphans
from codeagent_mcp.http_limits import SimpleRateLimitMiddleware


def test_redact_authorization() -> None:
    assert "[REDACTED]" in redact("Authorization: Bearer abc.def.ghi")
    assert "client_secret" not in redact(
        "client_secret=supersecretvalue"
    ).lower() or "[REDACTED]" in redact("client_secret=supersecretvalue")


def test_classify_failure_layers() -> None:
    assert classify_failure("SSL certificate verify failed") == "tls"
    assert classify_failure("Name or service not known") == "dns"
    assert classify_failure("Bad Gateway 502") == "proxy"
    assert classify_failure("caller sub is not authorized") == "oauth"
    assert classify_failure("something else") == "core"


def test_emit_audit_json(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="codeagent_mcp.audit")
    emit_audit({"event": "tool_call", "tool": "server_info", "ok": True, "sub": "github|1"})
    assert any("server_info" in r.message for r in caplog.records)


def test_redacting_filter() -> None:
    filt = RedactingFilter()
    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Authorization: Bearer tokensecret",
        args=(),
        exc_info=None,
    )
    assert filt.filter(record) is True
    assert "tokensecret" not in record.getMessage()


def test_cleanup_spool_ttl(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    old = spool / "old.log"
    old.write_text("x" * 100)
    # mtime in the past
    import os
    import time

    os.utime(old, (time.time() - 10_000, time.time() - 10_000))
    fresh = spool / "fresh.log"
    fresh.write_text("y")
    result = cleanup_spool(spool, ttl_s=60)
    assert result["removed"] == 1
    assert not old.exists()
    assert fresh.exists()


def test_detect_orphans(tmp_path: Path) -> None:
    leases = tmp_path / "leases.json"
    terms = tmp_path / "terminals.json"
    leases.write_text(
        json.dumps(
            {
                "leases": {
                    "a": {
                        "project": "demo",
                        "expires_at": "2099-01-01T00:00:00+00:00",
                    }
                }
            }
        )
    )
    terms.write_text(json.dumps({"terminals": {"t1": {"project": "other"}}}))
    out = detect_orphans(lease_store=leases, terminal_store=terms)
    assert "demo" in out["lease_without_terminals"]
    assert "other" in out["terminals_without_lease"]


def test_rate_limit_middleware() -> None:
    calls = {"n": 0}

    async def app(scope, receive, send):
        calls["n"] += 1
        body = b"ok"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", b"2")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    mw = SimpleRateLimitMiddleware(app, limit=2, window_s=60)

    async def _run() -> list[int]:

        statuses: list[int] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        for _ in range(3):
            status_box: dict[str, int] = {}

            async def send(message, box=status_box):
                if message["type"] == "http.response.start":
                    box["status"] = message["status"]

            await mw(
                {"type": "http", "path": "/mcp/", "client": ("1.2.3.4", 123)},
                receive,
                send,
            )
            statuses.append(status_box["status"])
        return statuses

    import asyncio

    statuses = asyncio.run(_run())
    assert statuses[:2] == [200, 200]
    assert statuses[2] == 429
