"""MCP tools: visual capture / get / compare."""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.tools.base import ToolResult
from fastmcp.utilities.types import Image

from codeagent_mcp.tools.annotations import MUT, RO
from codeagent_mcp.visual.service import get_visual_service, set_visual_service

CaptureMode = Literal["viewport", "element", "full_page"]
DevicePreset = Literal["desktop", "mobile"]


def _as_tool_result(result: Any) -> Any:
    """Wrap (meta, png_bytes) as ToolResult with ImageContent; pass through errors."""
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], dict):
        meta, png = result
        if not meta.get("ok"):
            return meta
        return ToolResult(
            content=[Image(data=png, format="png")],
            structured_content=meta,
        )
    return result


def register_visual_tools(server: FastMCP) -> None:
    @server.tool(
        name="visual_capture",
        description=(
            "Capture a PNG screenshot of the current browser page (viewport/element/full_page). "
            "Optional width/height override device presets (desktop/mobile). "
            "Returns metadata + MCP ImageContent. Requires lease_id and an open browser session. "
            "Does not navigate."
        ),
        annotations=MUT,
    )
    def visual_capture(
        lease_id: str,
        mode: CaptureMode = "viewport",
        selector: str | None = None,
        device: DevicePreset = "desktop",
        width: int | None = None,
        height: int | None = None,
    ) -> Any:
        return _as_tool_result(
            get_visual_service().capture(
                lease_id=lease_id,
                mode=mode,
                selector=selector,
                device=device,
                width=width,
                height=height,
                include_image=True,
            )
        )

    @server.tool(
        name="visual_get",
        description=(
            "Re-fetch a previously captured artifact by opaque artifact_id (within TTL). "
            "Returns metadata + ImageContent. Requires lease_id."
        ),
        annotations=RO,
    )
    def visual_get(lease_id: str, artifact_id: str) -> Any:
        return _as_tool_result(
            get_visual_service().get(lease_id=lease_id, artifact_id=artifact_id, include_image=True)
        )

    @server.tool(
        name="visual_compare",
        description=(
            "Pixel-diff two PNG artifacts. Returns metrics + a highlighted diff image. "
            "Sizes must match. Requires lease_id."
        ),
        annotations=MUT,
    )
    def visual_compare(
        lease_id: str,
        artifact_id_a: str,
        artifact_id_b: str,
        threshold: int = 0,
    ) -> Any:
        return _as_tool_result(
            get_visual_service().compare(
                lease_id=lease_id,
                artifact_id_a=artifact_id_a,
                artifact_id_b=artifact_id_b,
                threshold=threshold,
                include_image=True,
            )
        )


__all__ = ["register_visual_tools", "get_visual_service", "set_visual_service"]
