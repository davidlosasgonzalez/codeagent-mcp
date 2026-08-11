"""MCP tools: terminal lifecycle."""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from codeagent_mcp.terminal.service import KEY_MAP, TerminalService
from codeagent_mcp.terminal.spool import DEFAULT_MAX_READ_BYTES
from codeagent_mcp.tools.annotations import MUT, RO

KeyName = Literal[
    "ENTER",
    "CTRL_C",
    "CTRL_D",
    "TAB",
    "ESC",
    "UP",
    "DOWN",
    "LEFT",
    "RIGHT",
]

_SERVICE: TerminalService | None = None


def get_terminal_service() -> TerminalService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = TerminalService()
    return _SERVICE


def set_terminal_service(service: TerminalService | None) -> None:
    global _SERVICE
    _SERVICE = service


def register_terminal_tools(server: FastMCP) -> None:
    @server.tool(
        name="terminal_list",
        description=(
            "List managed terminals for the project of lease_id. "
            "Requires lease_id. Does not include output bodies."
        ),
        annotations=RO,
    )
    def terminal_list(lease_id: str) -> dict[str, Any]:
        return get_terminal_service().list(lease_id=lease_id)

    @server.tool(
        name="terminal_status",
        description=(
            "Status of one terminal by pane_id or alias (tmux metadata only). "
            "Returns SESSION_DEAD if the shell exited. Output via terminal_read/snapshot."
        ),
        annotations=RO,
    )
    def terminal_status(
        lease_id: str,
        pane_id: str | None = None,
        alias: str | None = None,
    ) -> dict[str, Any]:
        return get_terminal_service().status(lease_id=lease_id, pane_id=pane_id, alias=alias)

    @server.tool(
        name="terminal_create",
        description=(
            "Create a persistent bash PTY under the dedicated tmux socket. "
            "Requires lease_id. Aliases main/app/debug recommended. Max 3 per lease. "
            "Survives MCP process restart."
        ),
        annotations=MUT,
    )
    def terminal_create(
        lease_id: str,
        alias: str,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        return get_terminal_service().create(lease_id=lease_id, alias=alias, cwd=cwd)

    @server.tool(
        name="terminal_write",
        description=(
            "Send literal text to a terminal (no implicit Enter). "
            "Use terminal_key(ENTER) to submit. Requires lease_id."
        ),
        annotations=MUT,
    )
    def terminal_write(
        lease_id: str,
        text: str,
        pane_id: str | None = None,
        alias: str | None = None,
    ) -> dict[str, Any]:
        return get_terminal_service().write(
            lease_id=lease_id, text=text, pane_id=pane_id, alias=alias
        )

    @server.tool(
        name="terminal_key",
        description=(
            "Send a special key to a terminal. "
            f"Enum: {', '.join(sorted(KEY_MAP))}. Requires lease_id."
        ),
        annotations=MUT,
    )
    def terminal_key(
        lease_id: str,
        key: KeyName,
        pane_id: str | None = None,
        alias: str | None = None,
    ) -> dict[str, Any]:
        return get_terminal_service().key(lease_id=lease_id, key=key, pane_id=pane_id, alias=alias)

    @server.tool(
        name="terminal_interrupt",
        description=("Send TTY Ctrl+C to the pane (not kill -INT to pane_pid). Requires lease_id."),
        annotations=MUT,
    )
    def terminal_interrupt(
        lease_id: str,
        pane_id: str | None = None,
        alias: str | None = None,
    ) -> dict[str, Any]:
        return get_terminal_service().interrupt(lease_id=lease_id, pane_id=pane_id, alias=alias)

    @server.tool(
        name="terminal_close",
        description=(
            "Destroy a terminal pane and drop its registry entry. "
            "Active project lease may reclaim orphans. Does not kill the tmux server."
        ),
        annotations=MUT,
    )
    def terminal_close(
        lease_id: str,
        pane_id: str | None = None,
        alias: str | None = None,
    ) -> dict[str, Any]:
        return get_terminal_service().close(lease_id=lease_id, pane_id=pane_id, alias=alias)

    @server.tool(
        name="terminal_reset",
        description=(
            "Close alias if present, then create a fresh terminal with the same alias. "
            "Use after SESSION_DEAD or to reclaim."
        ),
        annotations=MUT,
    )
    def terminal_reset(
        lease_id: str,
        alias: str,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        return get_terminal_service().reset(lease_id=lease_id, alias=alias, cwd=cwd)

    @server.tool(
        name="terminal_read",
        description=(
            "Read incremental terminal output from the durable spool (pipe-pane). "
            "Pass next_cursor from the previous call; omit cursor to start at retained start. "
            "Text is ANSI-sanitized; cursor advances over raw bytes. Requires lease_id."
        ),
        annotations=RO,
    )
    def terminal_read(
        lease_id: str,
        cursor: str | None = None,
        max_bytes: int = DEFAULT_MAX_READ_BYTES,
        pane_id: str | None = None,
        alias: str | None = None,
    ) -> dict[str, Any]:
        return get_terminal_service().read(
            lease_id=lease_id,
            cursor=cursor,
            max_bytes=max_bytes,
            pane_id=pane_id,
            alias=alias,
        )

    @server.tool(
        name="terminal_snapshot",
        description=(
            "Capture visible pane (+scrollback) via capture-pane. "
            "Not an incremental log — use terminal_read for that. Requires lease_id."
        ),
        annotations=RO,
    )
    def terminal_snapshot(
        lease_id: str,
        pane_id: str | None = None,
        alias: str | None = None,
        include_history: bool = True,
    ) -> dict[str, Any]:
        return get_terminal_service().snapshot(
            lease_id=lease_id,
            pane_id=pane_id,
            alias=alias,
            include_history=include_history,
        )
