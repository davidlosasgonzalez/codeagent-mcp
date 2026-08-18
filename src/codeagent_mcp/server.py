"""MCP server factory: stdio (local/dev) + Streamable HTTP (loopback/remote adapter)."""

from __future__ import annotations

import os
from typing import Any, Literal

from fastmcp import FastMCP

from codeagent_mcp._version import __version__
from codeagent_mcp.logging_config import configure_logging
from codeagent_mcp.tools.annotations import RO
from codeagent_mcp.tools.browser import register_browser_tools
from codeagent_mcp.tools.exec_run import register_exec_tools
from codeagent_mcp.tools.fs import register_fs_tools
from codeagent_mcp.tools.git_tools import register_git_tools
from codeagent_mcp.tools.ops import register_ops_tools
from codeagent_mcp.tools.project import register_project_tools
from codeagent_mcp.tools.runtime import register_runtime_tools
from codeagent_mcp.tools.server_info import build_server_info
from codeagent_mcp.tools.service_control import register_service_control_tools
from codeagent_mcp.tools.terminal import register_terminal_tools
from codeagent_mcp.tools.visual import register_visual_tools
from codeagent_mcp.tools.workspace import register_workspace_tools

TransportName = Literal["stdio", "http"]

DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8765
DEFAULT_MCP_PATH = "/mcp/"


def create_server(
    *,
    transport: TransportName = "stdio",
    auth: Any | None = None,
    middleware: list | None = None,
) -> FastMCP:
    """Build the CodeAgent MCP server and register the current tool surface.

    ``transport`` is recorded in ``server_info`` only; callers choose how to run
    the returned :class:`~fastmcp.FastMCP` instance (stdio vs HTTP app).
    """
    server = FastMCP(
        name="codeagent-mcp",
        version=__version__,
        instructions=(
            "CodeAgent MCP: portable coding-agent backend. "
            "Use server_info first. Core has no ChatGPT runtime dependency. "
            "Project ids come from the server-side registry (CODEAGENT_PROJECTS_FILE). "
            "Use stable environment-specific identifiers; acquiring a lease is required "
            "before mutating tools. Demo smoke project id is typically 'demo'. "
            "If a broad tool listing reached you through a cache it may be older than "
            "this server: compare capabilities.tool_surface from server_info against "
            "the tools you hold, and re-discover a specific tool before concluding an "
            "argument does not exist. Tool results name arguments they expect you to "
            "use; trust that over a cached schema."
        ),
        auth=auth,
        middleware=middleware or [],
    )

    register_workspace_tools(server)
    register_exec_tools(server)
    register_fs_tools(server)
    register_git_tools(server)
    register_project_tools(server)
    register_terminal_tools(server)
    register_browser_tools(server)
    register_visual_tools(server)
    register_ops_tools(server)
    register_service_control_tools(server)
    register_runtime_tools(server)

    @server.tool(
        name="server_info",
        description=(
            "Return CodeAgent MCP version, build identity and capability summary. "
            "Use on first contact. Does not include secrets, env, or host paths. "
            "capabilities.tool_surface.count and .fingerprint describe the tools the "
            "server is publishing right now: if they disagree with the tool list you "
            "hold, your catalogue is stale — re-discover before concluding an argument "
            "or tool does not exist. build.commit/dirty identify the deployed code. "
            "Do not use for project orientation — that is project_bootstrap."
        ),
        annotations=RO,
    )
    async def server_info() -> dict:
        # Derive from the live FastMCP registry — never a parallel hardcoded list.
        tools = await server.list_tools(run_middleware=False)
        names = sorted(t.name for t in tools)
        surface = [
            {
                "name": tool.name,
                "properties": sorted((tool.parameters or {}).get("properties", {})),
                "required": sorted((tool.parameters or {}).get("required", [])),
            }
            for tool in tools
        ]
        return build_server_info(transport=transport, available_tools=names, tool_surface=surface)

    return server


def run_stdio() -> None:
    """Run the server on stdio (local/dev transport; must keep working)."""
    configure_logging()
    from codeagent_mcp.cleanup import run_startup_cleanup

    run_startup_cleanup()
    create_server(transport="stdio").run(transport="stdio")


def build_http_app(
    *,
    mcp_path: str = DEFAULT_MCP_PATH,
    require_auth: bool = True,
) -> Any:
    """Build the Streamable HTTP ASGI app (loopback bind; TLS terminates upstream).

    When ``require_auth`` is true, GitHub OAuth plus ``CODEAGENT_ALLOWED_SUBS``
    are required (fail-closed).
    """
    from codeagent_mcp.audit_middleware import AuditToolMiddleware
    from codeagent_mcp.auth import AllowlistSubMiddleware, build_github_auth, parse_allowed_subs
    from codeagent_mcp.http_limits import SimpleRateLimitMiddleware

    base_url = os.environ.get(
        "CODEAGENT_BASE_URL", f"http://{DEFAULT_HTTP_HOST}:{DEFAULT_HTTP_PORT}"
    )
    middleware: list = []
    auth = None
    if require_auth:
        auth = build_github_auth(base_url=base_url)
        allowed = parse_allowed_subs(os.environ.get("CODEAGENT_ALLOWED_SUBS"))
        if not allowed:
            raise SystemExit(
                "CODEAGENT_ALLOWED_SUBS is required when HTTP auth is enabled "
                "(fail-closed; OAuth alone must not authorize callers)"
            )
        middleware.append(AllowlistSubMiddleware(allowed))
    middleware.append(AuditToolMiddleware())

    server = create_server(transport="http", auth=auth, middleware=middleware)
    public_host = os.environ.get("CODEAGENT_HOST", "").strip()
    allowed_hosts = [public_host, f"*.{public_host}"] if public_host else None
    # Browser OAuth from ChatGPT sends Origin: https://chatgpt.com — must be allowlisted
    # or HostOriginGuardMiddleware returns 403 "Forbidden Origin" and the connect popup loops.
    default_origins = [
        "https://chatgpt.com",
        "https://chat.openai.com",
        "https://platform.openai.com",
    ]
    if public_host:
        default_origins = [
            f"https://{public_host}",
            f"http://{public_host}",
            *default_origins,
        ]
    extra = os.environ.get("CODEAGENT_ALLOWED_ORIGINS", "").strip()
    if extra:
        default_origins.extend(part.strip() for part in extra.split(",") if part.strip())
    app = server.http_app(
        path=mcp_path,
        stateless_http=True,
        json_response=True,
        host_origin_protection=True if allowed_hosts else "auto",
        allowed_hosts=allowed_hosts,
        allowed_origins=default_origins,
    )
    return SimpleRateLimitMiddleware(app)


def run_http(
    *,
    host: str = DEFAULT_HTTP_HOST,
    port: int = DEFAULT_HTTP_PORT,
    mcp_path: str = DEFAULT_MCP_PATH,
    require_auth: bool = True,
) -> None:
    """Serve Streamable HTTP. Default bind is loopback — never bind 0.0.0.0 here."""
    import uvicorn

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit(
            f"refusing non-loopback bind host={host!r}; "
            "public exposure must go through reverse proxy TLS only"
        )
    configure_logging()
    from codeagent_mcp.cleanup import run_startup_cleanup

    run_startup_cleanup()
    app = build_http_app(mcp_path=mcp_path, require_auth=require_auth)
    uvicorn.run(app, host=host, port=port, log_level="info")
