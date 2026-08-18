"""Read-only ``server_info`` payload builder (no secrets or host paths)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from codeagent_mcp._version import __version__
from codeagent_mcp.build_info import build_stamp

_PLANNED_TOOL_GROUPS: tuple[str, ...] = ()

FINGERPRINT_CHARS = 16


def tool_fingerprint(entries: Sequence[Mapping[str, Any]]) -> str:
    """Digest the tool surface a client would have cached.

    Names alone are not enough: the friction this exists for was a client
    holding a cached ``service_restart`` whose new ``wait_for_health_s``
    argument it could not see. So the input property names are part of the
    digest, and a client that compares this against its own view learns its
    catalogue is stale instead of concluding the capability does not exist.

    Descriptions are excluded on purpose — they are prose, and a wording fix
    should not look like a capability change.
    """
    canonical = sorted(
        (
            {
                "name": str(entry.get("name", "")),
                "properties": sorted(str(p) for p in entry.get("properties") or ()),
                "required": sorted(str(r) for r in entry.get("required") or ()),
            }
            for entry in entries
        ),
        key=lambda row: row["name"],
    )
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


def build_server_info(
    *,
    transport: str,
    available_tools: Sequence[str],
    tool_surface: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return non-sensitive server metadata and a capability summary.

    ``available_tools`` must come from the live MCP tool registry (or the same
    canonical source used to register tools). Do not maintain a parallel list.

    ``tool_surface`` carries the same tools with their input property names, so
    the reply can advertise a fingerprint. Omitting it yields a fingerprint of
    the names only, which is still stable but weaker.
    """
    entries = (
        list(tool_surface)
        if tool_surface is not None
        else [{"name": name} for name in available_tools]
    )
    return {
        "ok": True,
        "name": "codeagent-mcp",
        "version": __version__,
        "build": build_stamp(),
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
            "tool_surface": {
                "count": len(entries),
                "fingerprint": tool_fingerprint(entries),
            },
        },
    }
