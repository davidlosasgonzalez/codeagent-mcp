"""Read-only ``server_info`` payload builder (no secrets or host paths)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from codeagent_mcp._version import __version__

_PLANNED_TOOL_GROUPS: tuple[str, ...] = ()


def build_server_info(
    *,
    transport: str,
    available_tools: Sequence[str],
) -> dict[str, Any]:
    """Return non-sensitive server metadata and a capability summary.

    ``available_tools`` must come from the live MCP tool registry (or the same
    canonical source used to register tools). Do not maintain a parallel list.
    """
    return {
        "ok": True,
        "name": "codeagent-mcp",
        "version": __version__,
        "transport": transport,
        "product": "CodeAgent MCP",
        "layers": {
            "core": "portable MCP runtime (FastMCP)",
            "adaptation": "ChatGPT-first ergonomics and remote HTTPS/OAuth adapter",
            "targets": "configured project roots",
        },
        "capabilities": {
            "available_tools": list(available_tools),
            "planned_tool_groups": list(_PLANNED_TOOL_GROUPS),
        },
    }
