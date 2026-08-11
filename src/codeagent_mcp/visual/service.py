"""Screenshot capture and pixel diff."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any, Literal, cast

from PIL import Image as PILImage
from PIL import ImageChops

if TYPE_CHECKING:
    from playwright.sync_api import ViewportSize

from codeagent_mcp.artifact_store.store import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_PIXELS,
    ArtifactStore,
)
from codeagent_mcp.browser.service import (
    DEFAULT_VIEWPORT,
    get_browser_service,
)
from codeagent_mcp.errors import tool_error, tool_ok
from codeagent_mcp.tools.workspace import get_lease_manager

CaptureMode = Literal["viewport", "element", "full_page"]
DevicePreset = Literal["desktop", "mobile"]

MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}


class VisualService:
    def __init__(self, store: ArtifactStore | None = None) -> None:
        self.store = store or ArtifactStore()

    def _lease(self, lease_id: str) -> dict[str, Any]:
        return get_lease_manager().require_active(lease_id=lease_id)

    @staticmethod
    def _lease_err(lease: dict[str, Any]) -> dict[str, Any] | None:
        return None if lease.get("ok") else lease

    def capture(
        self,
        *,
        lease_id: str,
        mode: CaptureMode = "viewport",
        selector: str | None = None,
        device: DevicePreset = "desktop",
        width: int | None = None,
        height: int | None = None,
        include_image: bool = True,
    ) -> dict[str, Any] | tuple[dict[str, Any], bytes]:
        lease = self._lease(lease_id)
        if err := self._lease_err(lease):
            return err
        browser = get_browser_service()
        if browser._page is None:
            return tool_error(
                "NOT_FOUND",
                "no browser session; call browser_ensure and browser_open first",
                retryable=False,
            )
        denied = browser._authorize(lease)
        if denied:
            return denied

        page = browser._page
        prev_viewport = page.viewport_size
        try:
            if width is not None or height is not None:
                viewport = browser._normalize_viewport(width, height)
                if isinstance(viewport, dict) and viewport.get("ok") is False:
                    return viewport
                page.set_viewport_size(cast("ViewportSize", viewport))
            elif device == "mobile":
                page.set_viewport_size(MOBILE_VIEWPORT)
            elif device == "desktop":
                page.set_viewport_size(DEFAULT_VIEWPORT)

            if mode == "element":
                if not selector:
                    return tool_error(
                        "INVALID_ARGUMENT",
                        "selector required for mode=element",
                        retryable=False,
                    )
                loc = page.locator(selector).first
                png = loc.screenshot(type="png")
            elif mode == "full_page":
                png = page.screenshot(type="png", full_page=True)
            else:
                png = page.screenshot(type="png", full_page=False)
        except Exception as exc:
            return tool_error("TIMEOUT", f"capture failed: {exc}", retryable=True)
        finally:
            if prev_viewport:
                try:
                    page.set_viewport_size(prev_viewport)
                except Exception:
                    pass

        try:
            img = PILImage.open(io.BytesIO(png)).convert("RGBA")
        except Exception as exc:
            return tool_error("INTERNAL_ERROR", f"invalid png: {exc}", retryable=False)

        w, h = img.size
        if w * h > DEFAULT_MAX_PIXELS:
            return tool_error(
                "OUTPUT_LIMIT",
                f"capture {w}x{h} exceeds pixel cap",
                retryable=False,
            )
        if len(png) > DEFAULT_MAX_BYTES:
            # shrink once
            scale = (DEFAULT_MAX_BYTES / len(png)) ** 0.5
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            img = img.resize((nw, nh))
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            png = buf.getvalue()
            w, h = img.size
            resized = True
            if len(png) > DEFAULT_MAX_BYTES:
                return tool_error(
                    "OUTPUT_LIMIT",
                    "capture still exceeds byte cap after resize",
                    retryable=False,
                )
        else:
            resized = False

        try:
            art = self.store.put_png(
                png,
                width=w,
                height=h,
                kind=f"capture:{mode}:{device}",
                lease_id=lease["lease_id"],
            )
        except ValueError as exc:
            return tool_error("OUTPUT_LIMIT", str(exc), retryable=False)
        except RuntimeError as exc:
            return tool_error("RISK_BLOCKED", str(exc), retryable=True)

        meta = tool_ok(
            artifact_id=art.artifact_id,
            mime_type="image/png",
            width=art.width,
            height=art.height,
            size_bytes=art.size_bytes,
            mode=mode,
            device=device,
            resized=resized,
            expires_at=art.expires_at,
            url=page.url,
        )
        if include_image:
            return meta, png
        return meta

    def get(self, *, lease_id: str, artifact_id: str, include_image: bool = True):
        lease = self._lease(lease_id)
        if err := self._lease_err(lease):
            return err
        art = self.store.get(artifact_id)
        if art is None:
            return tool_error(
                "NOT_FOUND",
                "artifact unknown or expired",
                retryable=False,
                next_action="Call visual_capture again",
            )
        meta = tool_ok(
            artifact_id=art.artifact_id,
            mime_type=art.mime_type,
            width=art.width,
            height=art.height,
            size_bytes=art.size_bytes,
            kind=art.kind,
            expires_at=art.expires_at,
        )
        if include_image:
            return meta, art.path.read_bytes()
        return meta

    def compare(
        self,
        *,
        lease_id: str,
        artifact_id_a: str,
        artifact_id_b: str,
        threshold: int = 0,
        include_image: bool = True,
    ):
        lease = self._lease(lease_id)
        if err := self._lease_err(lease):
            return err
        a = self.store.get(artifact_id_a)
        b = self.store.get(artifact_id_b)
        if a is None or b is None:
            return tool_error(
                "NOT_FOUND",
                "one or both artifacts unknown/expired",
                retryable=False,
            )
        img_a = PILImage.open(a.path).convert("RGB")
        img_b = PILImage.open(b.path).convert("RGB")
        if img_a.size != img_b.size:
            return tool_error(
                "INVALID_ARGUMENT",
                f"size mismatch {img_a.size} vs {img_b.size}",
                retryable=False,
            )
        diff = ImageChops.difference(img_a, img_b)
        # count differing pixels
        w, h = img_a.size
        total = w * h
        # threshold on channel max
        px = diff.load()
        changed = 0
        highlight = PILImage.new("RGB", (w, h), (0, 0, 0))
        draw_src = img_a.copy()
        hp = highlight.load()
        ap = draw_src.load()
        assert px is not None and hp is not None and ap is not None
        for y in range(h):
            for x in range(w):
                r, g, bch = cast("tuple[int, int, int]", px[x, y])
                if max(r, g, bch) > threshold:
                    changed += 1
                    hp[x, y] = (255, 0, 0)
                    # tint original
                    or_, og, ob = cast("tuple[int, int, int]", ap[x, y])
                    ap[x, y] = (min(255, or_ + 80), og // 2, ob // 2)
        ratio = changed / total if total else 0.0
        # compose diff image: side-by-side original tint + pure diff
        out = PILImage.new("RGB", (w * 2, h))
        out.paste(draw_src, (0, 0))
        out.paste(highlight, (w, 0))
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
        diff_png = buf.getvalue()
        if len(diff_png) > DEFAULT_MAX_BYTES:
            return tool_error("OUTPUT_LIMIT", "diff png too large", retryable=False)
        try:
            art = self.store.put_png(
                diff_png,
                width=w * 2,
                height=h,
                kind="diff",
                lease_id=lease["lease_id"],
            )
        except (ValueError, RuntimeError) as exc:
            return tool_error("OUTPUT_LIMIT", str(exc), retryable=False)

        meta = tool_ok(
            artifact_id=art.artifact_id,
            artifact_id_a=artifact_id_a,
            artifact_id_b=artifact_id_b,
            width=w,
            height=h,
            pixels_changed=changed,
            pixels_total=total,
            change_ratio=round(ratio, 6),
            identical=changed == 0,
            threshold=threshold,
            size_bytes=art.size_bytes,
            expires_at=art.expires_at,
        )
        if include_image:
            return meta, diff_png
        return meta


_SERVICE: VisualService | None = None


def get_visual_service() -> VisualService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = VisualService()
    return _SERVICE


def set_visual_service(service: VisualService | None) -> None:
    global _SERVICE
    _SERVICE = service
