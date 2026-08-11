"""Audit + allowlist-aware tool middleware."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp.server.middleware import Middleware, MiddlewareContext

from codeagent_mcp.audit import classify_failure, emit_audit


class AuditToolMiddleware(Middleware):
    """Log tool name, lease/pane ids, sub, authz result — never tokens or transcripts."""

    async def on_call_tool(self, context: MiddlewareContext, call_next: Callable[..., Any]) -> Any:
        tool_name = getattr(getattr(context, "message", None), "name", None) or "unknown"
        arguments = getattr(getattr(context, "message", None), "arguments", None) or {}
        lease_id = None
        pane_id = None
        if isinstance(arguments, dict):
            lease_id = arguments.get("lease_id")
            pane_id = arguments.get("pane_id") or arguments.get("alias")

        sub = None
        login = None
        try:
            from fastmcp.server.dependencies import get_access_token

            token = get_access_token()
            if token is not None:
                claims = getattr(token, "claims", None) or {}
                sub = claims.get("sub")
                login = claims.get("login") or claims.get("preferred_username")
        except Exception:
            pass

        try:
            result = await call_next(context)
        except PermissionError as exc:
            emit_audit(
                {
                    "event": "tool_call",
                    "tool": tool_name,
                    "lease_id": lease_id,
                    "pane_id": pane_id,
                    "sub": sub,
                    "login": login,
                    "authz": "deny",
                    "error_code": "AUTHORIZATION_DENIED",
                    "layer": "oauth",
                    "ok": False,
                    "message": str(exc)[:200],
                }
            )
            raise
        except Exception as exc:
            emit_audit(
                {
                    "event": "tool_call",
                    "tool": tool_name,
                    "lease_id": lease_id,
                    "pane_id": pane_id,
                    "sub": sub,
                    "login": login,
                    "authz": "allow",
                    "error_code": type(exc).__name__,
                    "layer": classify_failure(str(exc)),
                    "ok": False,
                    "message": str(exc)[:200],
                }
            )
            raise

        ok = True
        error_code = None
        payload = result
        # FastMCP wraps tool returns in ToolResult; unwrap structured_content for ok/error.
        sc = getattr(result, "structured_content", None)
        if isinstance(sc, dict):
            payload = sc
        if isinstance(payload, dict) and payload.get("ok") is False:
            ok = False
            err = payload.get("error") or {}
            if isinstance(err, dict):
                error_code = err.get("code")
        if getattr(result, "is_error", False):
            ok = False
        emit_audit(
            {
                "event": "tool_call",
                "tool": tool_name,
                "lease_id": lease_id,
                "pane_id": pane_id,
                "sub": sub,
                "login": login,
                "authz": "allow",
                "error_code": error_code,
                "layer": classify_failure(error_code) if error_code else "core",
                "ok": ok,
            }
        )
        return result
