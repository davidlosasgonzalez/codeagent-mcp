"""In-process HTTP rate limit for Streamable HTTP."""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from typing import Any


class SimpleRateLimitMiddleware:
    """ASGI middleware: sliding window per client host on /mcp/."""

    def __init__(
        self, app: Any, *, limit: int | None = None, window_s: float | None = None
    ) -> None:
        self.app = app
        self.limit = int(limit or os.environ.get("CODEAGENT_HTTP_RATE_LIMIT", "120"))
        self.window_s = float(window_s or os.environ.get("CODEAGENT_HTTP_RATE_WINDOW_S", "60"))
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if not path.startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        client = (scope.get("client") or ("unknown", 0))[0]
        now = time.monotonic()
        q = self._hits[client]
        while q and now - q[0] > self.window_s:
            q.popleft()
        if len(q) >= self.limit:
            body = b'{"error":"rate_limited"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"retry-after", b"1"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        q.append(now)
        await self.app(scope, receive, send)
