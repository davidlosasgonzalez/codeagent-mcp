"""Base logging without secret fields."""

from __future__ import annotations

import logging
import os

from codeagent_mcp.audit import RedactingFilter

_SECRET_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def configure_logging(level: str | None = None) -> None:
    """Configure root logging for the MCP process.

    Never attach environment dumps. Level from CODEAGENT_LOG_LEVEL or INFO.
    """
    resolved = (level or os.environ.get("CODEAGENT_LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, resolved, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    redactor = RedactingFilter()
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(redactor)
    logging.getLogger("codeagent_mcp.audit").setLevel(logging.INFO)
    logging.getLogger("codeagent_mcp").debug(
        "logging configured; secret-like env keys present=%s",
        sum(1 for k in os.environ if any(m in k.upper() for m in _SECRET_ENV_MARKERS)),
    )
