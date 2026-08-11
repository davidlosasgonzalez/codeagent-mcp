"""Structured recoverable tool errors."""

from __future__ import annotations

from typing import Any


def tool_error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    next_action: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    err: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": retryable,
    }
    if next_action:
        err["next_action"] = next_action
    err.update(extra)
    return {"ok": False, "error": err}


def tool_ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}
