"""Browser session service — Playwright Chromium, not durable across MCP death."""

from __future__ import annotations

import atexit
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from playwright.sync_api import ViewportSize

from codeagent_mcp.browser.urls import validate_navigation_url
from codeagent_mcp.errors import tool_error, tool_ok
from codeagent_mcp.tools.workspace import get_lease_manager

ActionName = Literal["click", "fill", "press", "select", "wait"]

DEFAULT_VIEWPORT: ViewportSize = {"width": 1280, "height": 720}
MAX_VIEWPORT_WIDTH = 3840
MAX_VIEWPORT_HEIGHT = 2160
MIN_VIEWPORT = 1
DEFAULT_PROFILE = "/var/lib/codeagent-mcp/browser-profile"
DEFAULT_BROWSERS = "/var/lib/codeagent-mcp/playwright"
MAX_SNAPSHOT_CHARS = 20_000
MAX_CONSOLE = 50
MAX_A11Y_NODES = 80

DOM_SUMMARY_JS = """() => {
  const q = (s) => [...document.querySelectorAll(s)];
  return {
    title: document.title,
    headings: q("h1,h2,h3").slice(0, 20).map((e) => ({
      tag: e.tagName.toLowerCase(),
      text: (e.innerText || "").slice(0, 120),
    })),
    buttons: q("button,[role=button],input[type=submit]").slice(0, 30).map((e) => ({
      text: (e.innerText || e.value || "").slice(0, 80),
      id: e.id || null,
      name: e.getAttribute("name"),
    })),
    inputs: q("input,textarea,select").slice(0, 30).map((e) => ({
      tag: e.tagName.toLowerCase(),
      type: e.getAttribute("type"),
      name: e.getAttribute("name"),
      id: e.id || null,
      placeholder: e.getAttribute("placeholder"),
    })),
    links: q("a[href]").slice(0, 30).map((e) => ({
      text: (e.innerText || "").slice(0, 80),
      href: e.getAttribute("href"),
    })),
  };
}"""


class BrowserService:
    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._owner_lease_id: str | None = None
        self._console: list[dict[str, str]] = []
        self._page_errors: list[str] = []
        atexit.register(self.shutdown)

    def shutdown(self) -> None:
        for obj, closer in (
            (self._context, "close"),
            (self._browser, "close"),
            (self._playwright, "stop"),
        ):
            if obj is None:
                continue
            try:
                getattr(obj, closer)()
            except Exception:
                pass
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._owner_lease_id = None
        self._console.clear()
        self._page_errors.clear()

    def _require_lease(self, lease_id: str) -> dict[str, Any]:
        return get_lease_manager().require_active(lease_id=lease_id)

    @staticmethod
    def _lease_err(lease: dict[str, Any]) -> dict[str, Any] | None:
        if not lease.get("ok"):
            return lease
        return None

    def _authorize(self, lease: dict[str, Any]) -> dict[str, Any] | None:
        if self._owner_lease_id and self._owner_lease_id != lease["lease_id"]:
            return tool_error(
                "AUTHORIZATION_DENIED",
                "browser session owned by a different lease; call browser_ensure to reclaim",
                retryable=False,
            )
        return None

    @staticmethod
    def _normalize_viewport(
        width: int | None, height: int | None, *, fallback: dict[str, int] | None = None
    ) -> dict[str, Any] | dict[str, int]:
        base = dict(fallback or DEFAULT_VIEWPORT)
        w = base["width"] if width is None else width
        h = base["height"] if height is None else height
        if not isinstance(w, int) or not isinstance(h, int):
            return tool_error(
                "INVALID_ARGUMENT",
                "viewport width/height must be integers",
                retryable=False,
            )
        if not (MIN_VIEWPORT <= w <= MAX_VIEWPORT_WIDTH):
            return tool_error(
                "INVALID_ARGUMENT",
                f"width must be {MIN_VIEWPORT}..{MAX_VIEWPORT_WIDTH}",
                retryable=False,
            )
        if not (MIN_VIEWPORT <= h <= MAX_VIEWPORT_HEIGHT):
            return tool_error(
                "INVALID_ARGUMENT",
                f"height must be {MIN_VIEWPORT}..{MAX_VIEWPORT_HEIGHT}",
                retryable=False,
            )
        return {"width": w, "height": h}

    def ensure(
        self,
        *,
        lease_id: str,
        force: bool = False,
        width: int | None = None,
        height: int | None = None,
    ) -> dict[str, Any]:
        lease = self._require_lease(lease_id)
        if err := self._lease_err(lease):
            return err
        viewport = self._normalize_viewport(width, height)
        if isinstance(viewport, dict) and viewport.get("ok") is False:
            return viewport

        if self._page is not None and not force:
            if self._authorize(lease):
                # reclaim: kill and recreate
                self.shutdown()
            else:
                if width is not None or height is not None:
                    try:
                        self._page.set_viewport_size(cast("ViewportSize", viewport))
                    except Exception as exc:
                        return tool_error(
                            "INTERNAL_ERROR",
                            f"failed to set viewport: {exc}",
                            retryable=True,
                        )
                current = self._page.viewport_size or viewport
                return tool_ok(
                    status="ready",
                    reused=True,
                    viewport=current,
                    url=self._page.url,
                )

        self.shutdown()
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", DEFAULT_BROWSERS)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            return tool_error(
                "UNSUPPORTED_BINARY",
                f"playwright not installed: {exc}",
                retryable=False,
            )

        profile = Path(os.environ.get("CODEAGENT_BROWSER_PROFILE", DEFAULT_PROFILE))
        profile.mkdir(parents=True, mode=0o700, exist_ok=True)

        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage"],
            )
            self._context = self._browser.new_context(
                viewport=cast("ViewportSize", viewport),
                ignore_https_errors=True,
            )
            self._page = self._context.new_page()
            self._console.clear()
            self._page_errors.clear()

            def on_console(msg) -> None:
                if len(self._console) >= MAX_CONSOLE:
                    return
                self._console.append({"type": msg.type, "text": msg.text[:500]})

            def on_page_error(exc) -> None:
                if len(self._page_errors) >= MAX_CONSOLE:
                    return
                self._page_errors.append(str(exc)[:500])

            self._page.on("console", on_console)
            self._page.on("pageerror", on_page_error)
        except Exception as exc:  # boundary: browser launch
            self.shutdown()
            return tool_error(
                "INTERNAL_ERROR",
                f"failed to launch chromium: {exc}",
                retryable=True,
            )

        self._owner_lease_id = lease["lease_id"]
        return tool_ok(
            status="ready",
            reused=False,
            viewport=viewport,
            url="about:blank",
        )

    def open(self, *, lease_id: str, url: str) -> dict[str, Any]:
        lease = self._require_lease(lease_id)
        if err := self._lease_err(lease):
            return err
        if self._page is None:
            ensured = self.ensure(lease_id=lease_id)
            if not ensured.get("ok"):
                return ensured
        if denied := self._authorize(lease):
            return denied
        try:
            target = validate_navigation_url(url)
        except ValueError as exc:
            return tool_error("RISK_BLOCKED", str(exc), retryable=False)

        assert self._page is not None
        try:
            self._page.goto(target, wait_until="domcontentloaded", timeout=30_000)
            final = self._page.url
            validate_navigation_url(final)
        except ValueError as exc:
            return tool_error(
                "RISK_BLOCKED",
                f"navigation left allowlist: {exc}",
                retryable=False,
                final_url=getattr(self._page, "url", None),
            )
        except Exception as exc:
            return tool_error("TIMEOUT", f"navigation failed: {exc}", retryable=True)

        return tool_ok(url=self._page.url, title=self._page.title())

    def action(
        self,
        *,
        lease_id: str,
        action: ActionName,
        selector: str | None = None,
        value: str | None = None,
        key: str | None = None,
        timeout_ms: int = 10_000,
    ) -> dict[str, Any]:
        lease = self._require_lease(lease_id)
        if err := self._lease_err(lease):
            return err
        if self._page is None:
            return tool_error(
                "NOT_FOUND",
                "no browser session; call browser_ensure first",
                retryable=False,
            )
        if denied := self._authorize(lease):
            return denied

        page = self._page
        try:
            if action == "click":
                if not selector:
                    return tool_error(
                        "INVALID_ARGUMENT", "selector required for click", retryable=False
                    )
                page.locator(selector).first.click(timeout=timeout_ms)
            elif action == "fill":
                if not selector:
                    return tool_error(
                        "INVALID_ARGUMENT", "selector required for fill", retryable=False
                    )
                if value is None:
                    return tool_error(
                        "INVALID_ARGUMENT", "value required for fill", retryable=False
                    )
                page.locator(selector).first.fill(value, timeout=timeout_ms)
            elif action == "press":
                if not key:
                    return tool_error("INVALID_ARGUMENT", "key required for press", retryable=False)
                if selector:
                    page.locator(selector).first.press(key, timeout=timeout_ms)
                else:
                    page.keyboard.press(key)
            elif action == "select":
                if not selector or value is None:
                    return tool_error(
                        "INVALID_ARGUMENT",
                        "selector and value required for select",
                        retryable=False,
                    )
                page.locator(selector).first.select_option(value, timeout=timeout_ms)
            elif action == "wait":
                if selector:
                    page.locator(selector).first.wait_for(state="visible", timeout=timeout_ms)
                else:
                    page.wait_for_timeout(min(timeout_ms, 5_000))
            else:
                return tool_error("INVALID_ARGUMENT", f"unknown action {action!r}", retryable=False)
        except Exception as exc:
            return tool_error(
                "TIMEOUT",
                f"action {action} failed: {exc}",
                retryable=True,
                next_action="Retry with different selector or call browser_snapshot",
            )

        # re-validate URL after action (redirects)
        try:
            validate_navigation_url(page.url)
        except ValueError as exc:
            return tool_error("RISK_BLOCKED", f"page left allowlist: {exc}", retryable=False)

        return tool_ok(action=action, url=page.url, title=page.title())

    def set_viewport(self, *, lease_id: str, width: int, height: int) -> dict[str, Any]:
        lease = self._require_lease(lease_id)
        if err := self._lease_err(lease):
            return err
        if self._page is None:
            return tool_error(
                "NOT_FOUND",
                "no browser session; call browser_ensure first",
                retryable=False,
            )
        if denied := self._authorize(lease):
            return denied
        viewport = self._normalize_viewport(width, height)
        if isinstance(viewport, dict) and viewport.get("ok") is False:
            return viewport
        try:
            self._page.set_viewport_size(cast("ViewportSize", viewport))
        except Exception as exc:
            return tool_error(
                "INTERNAL_ERROR",
                f"failed to set viewport: {exc}",
                retryable=True,
            )
        return tool_ok(viewport=self._page.viewport_size or viewport, url=self._page.url)

    def reload(
        self, *, lease_id: str, ignore_cache: bool = False, timeout_ms: int = 30_000
    ) -> dict[str, Any]:
        lease = self._require_lease(lease_id)
        if err := self._lease_err(lease):
            return err
        if self._page is None:
            return tool_error(
                "NOT_FOUND",
                "no browser session; call browser_ensure first",
                retryable=False,
            )
        if denied := self._authorize(lease):
            return denied
        page = self._page
        cdp = None
        try:
            if ignore_cache and self._context is not None:
                cdp = self._context.new_cdp_session(page)
                cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
            page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
            final = page.url
            validate_navigation_url(final)
        except ValueError as exc:
            return tool_error("RISK_BLOCKED", f"reload left allowlist: {exc}", retryable=False)
        except Exception as exc:
            return tool_error("TIMEOUT", f"reload failed: {exc}", retryable=True)
        finally:
            if cdp is not None:
                try:
                    cdp.send("Network.setCacheDisabled", {"cacheDisabled": False})
                except Exception:
                    pass
                try:
                    cdp.detach()
                except Exception:
                    pass
        return tool_ok(
            url=page.url,
            title=page.title(),
            ignore_cache=ignore_cache,
            viewport=page.viewport_size,
        )

    def snapshot(self, *, lease_id: str) -> dict[str, Any]:
        lease = self._require_lease(lease_id)
        if err := self._lease_err(lease):
            return err
        if self._page is None:
            return tool_error(
                "NOT_FOUND",
                "no browser session; call browser_ensure first",
                retryable=False,
            )
        if denied := self._authorize(lease):
            return denied

        page = self._page
        if page.url.startswith(("http://", "https://")):
            try:
                validate_navigation_url(page.url)
            except ValueError as exc:
                return tool_error("RISK_BLOCKED", str(exc), retryable=False)

        a11y: Any = None
        truncated = False
        try:
            a11y = page.accessibility.snapshot()  # pyright: ignore[reportAttributeAccessIssue]
        except Exception:
            a11y = None

        flags = {"truncated": False}

        def trim_a11y(node: Any, *, budget: list[int]) -> Any:
            if not isinstance(node, dict) or budget[0] <= 0:
                flags["truncated"] = True
                return None
            budget[0] -= 1
            out = {k: node.get(k) for k in ("role", "name", "value") if node.get(k)}
            children = node.get("children") or []
            trimmed_children = []
            for child in children:
                if budget[0] <= 0:
                    flags["truncated"] = True
                    break
                t = trim_a11y(child, budget=budget)
                if t:
                    trimmed_children.append(t)
            if trimmed_children:
                out["children"] = trimmed_children
            return out

        budget = [MAX_A11Y_NODES]
        tree = trim_a11y(a11y, budget=budget) if a11y else None
        truncated = flags["truncated"]

        # lightweight DOM summary
        try:
            dom = page.evaluate(DOM_SUMMARY_JS)
        except Exception as exc:
            dom = {"error": str(exc)}

        import json

        payload = {
            "url": page.url,
            "title": page.title(),
            "dom": dom,
            "accessibility": tree,
            "console": list(self._console[-MAX_CONSOLE:]),
            "page_errors": list(self._page_errors[-MAX_CONSOLE:]),
            "truncated": truncated,
        }
        raw = json.dumps(payload, ensure_ascii=False)
        if len(raw) > MAX_SNAPSHOT_CHARS:
            payload["truncated"] = True
            payload["accessibility"] = None
            payload["note"] = "accessibility omitted to stay under size cap"
        return tool_ok(**payload)


_SERVICE: BrowserService | None = None


def get_browser_service() -> BrowserService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = BrowserService()
    return _SERVICE


def set_browser_service(service: BrowserService | None) -> None:
    global _SERVICE
    if _SERVICE is not None and service is not _SERVICE:
        _SERVICE.shutdown()
    _SERVICE = service
