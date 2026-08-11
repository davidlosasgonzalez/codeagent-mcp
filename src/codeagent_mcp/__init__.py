"""CodeAgent MCP — portable coding-agent backend."""

from __future__ import annotations

import argparse

from codeagent_mcp._version import __version__

__all__ = ["__version__", "main"]


def main(argv: list[str] | None = None) -> None:
    """CLI entry: stdio (default) or loopback HTTP."""
    parser = argparse.ArgumentParser(prog="codeagent-mcp")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="stdio for local/dev; http for loopback Streamable HTTP",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (loopback only)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP bind port")
    parser.add_argument("--mcp-path", default="/mcp/", help="MCP mount path")
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="HTTP without OAuth (loopback testing only; never expose publicly)",
    )
    args = parser.parse_args(argv)

    if args.transport == "stdio":
        from codeagent_mcp.server import run_stdio

        run_stdio()
        return

    from codeagent_mcp.server import run_http

    run_http(
        host=args.host,
        port=args.port,
        mcp_path=args.mcp_path,
        require_auth=not args.no_auth,
    )
