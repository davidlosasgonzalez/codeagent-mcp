"""MCP tools: browser bridge."""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from codeagent_mcp.browser.service import get_browser_service, set_browser_service
from codeagent_mcp.tools.annotations import MUT, RO
from codeagent_mcp.tools.workspace import get_lease_manager

ActionName = Literal["click", "fill", "press", "select", "wait"]


def register_browser_tools(server: FastMCP) -> None:
    @server.tool(
        name="browser_ensure",
        description=(
            "Start or reuse an isolated headless Chromium session (loopback browsing). "
            "Optional width/height set the viewport (defaults 1280x720). "
            "Requires lease_id. Not durable across MCP process death — unlike tmux. "
            "Screenshots are visual_capture, not this tool."
        ),
        annotations=MUT,
    )
    def browser_ensure(
        lease_id: str,
        force: bool = False,
        width: int | None = None,
        height: int | None = None,
    ) -> dict[str, Any]:
        return get_browser_service().ensure(
            lease_id=lease_id, force=force, width=width, height=height
        )

    @server.tool(
        name="browser_set_viewport",
        description=(
            "Set an arbitrary viewport size on the current browser session "
            "(width/height integers; max 3840x2160). Requires lease_id."
        ),
        annotations=MUT,
    )
    def browser_set_viewport(lease_id: str, width: int, height: int) -> dict[str, Any]:
        return get_browser_service().set_viewport(lease_id=lease_id, width=width, height=height)

    @server.tool(
        name="browser_reload",
        description=(
            "Reload the current page. Set ignore_cache=true for a hard reload "
            "(disables HTTP cache via CDP for that reload). Requires lease_id."
        ),
        annotations=MUT,
    )
    def browser_reload(
        lease_id: str,
        ignore_cache: bool = False,
        timeout_ms: int = 30_000,
    ) -> dict[str, Any]:
        return get_browser_service().reload(
            lease_id=lease_id, ignore_cache=ignore_cache, timeout_ms=timeout_ms
        )

    @server.tool(
        name="browser_open",
        description=(
            "Navigate to a loopback http(s) URL (127.0.0.1 / localhost). "
            "Blocks file:// and off-loopback hosts. Requires lease_id."
        ),
        annotations=MUT,
    )
    def browser_open(lease_id: str, url: str) -> dict[str, Any]:
        return get_browser_service().open(lease_id=lease_id, url=url)

    @server.tool(
        name="browser_action",
        description=(
            "Perform one bounded action: click, fill, press, select, or wait. "
            "Requires lease_id and usually a CSS selector."
        ),
        annotations=MUT,
    )
    def browser_action(
        lease_id: str,
        action: ActionName,
        selector: str | None = None,
        value: str | None = None,
        key: str | None = None,
        timeout_ms: int = 10_000,
    ) -> dict[str, Any]:
        return get_browser_service().action(
            lease_id=lease_id,
            action=action,
            selector=selector,
            value=value,
            key=key,
            timeout_ms=timeout_ms,
        )

    @server.tool(
        name="browser_close",
        description=(
            "Close the browser this lease opened, freeing its processes. "
            "Closing when nothing is running is success, not an error. "
            "Call it when finished with the browser; releasing the lease also closes it."
        ),
        annotations=MUT,
    )
    def browser_close(lease_id: str) -> dict[str, Any]:
        lease = get_lease_manager().require_active(lease_id=lease_id)
        if not lease.get("ok"):
            return lease
        return get_browser_service().close(lease_id=str(lease["lease_id"]))

    @server.tool(
        name="browser_snapshot",
        description=(
            "Structured page summary: DOM highlights, accessibility tree (capped), "
            "console and page errors. No screenshots (use visual_capture). Requires lease_id."
        ),
        annotations=RO,
    )
    def browser_snapshot(lease_id: str) -> dict[str, Any]:
        return get_browser_service().snapshot(lease_id=lease_id)


__all__ = ["register_browser_tools", "get_browser_service", "set_browser_service"]
