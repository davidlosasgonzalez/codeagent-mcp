# Visual capture

PNG screenshots and pixel diffs for ChatGPT vision. Builds on the browser bridge ([`browser.md`](browser.md)).

## Tools

| Tool | Role |
|------|------|
| `visual_capture` | PNG of viewport / element / full_page (+ desktop/mobile viewport) |
| `visual_get` | Re-emit artifact by opaque id within TTL |
| `visual_compare` | Pixel diff + metrics + highlighted PNG |

Returns **MCP ImageContent** (via FastMCP `Image`) plus structured metadata (`artifact_id`, dims, bytes).

## Artifacts

- Root: `CODEAGENT_ARTIFACT_ROOT` (default `/var/lib/codeagent-mcp/artifacts`)
- Opaque hex ids; TTL default 1h; global quota ~80MB; max ~2MB / 8MP per image
- Never stored under a registered project root

## Boundary

- `browser_snapshot` = DOM text; `visual_capture` = pixels
- Capture does not navigate; requires `browser_ensure` + open page
- Smoke: use a registered id such as `demo`
