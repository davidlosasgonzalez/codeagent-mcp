# CodeAgent MCP documentation

Reference documentation for operators self-hosting CodeAgent MCP. Start with the top-level [`README.md`](../README.md) for what the project is and how to install it.

## Product

| Page | Purpose |
|------|---------|
| [`product/clients.md`](product/clients.md) | Which MCP clients work, and how they connect |
| [`product/connect-chatgpt.md`](product/connect-chatgpt.md) | Step-by-step ChatGPT connector setup |
| [`product/tool-surface.md`](product/tool-surface.md) | Deployed MCP tools (39 Core) |
| [`product/tool-catalog.json`](product/tool-catalog.json) | Machine-readable `tools/list` snapshot |
| [`product/projects-registry.md`](product/projects-registry.md) | What roots the agent may touch (`projects.yaml`) |
| [`product/workspace-lease.md`](product/workspace-lease.md) | Exclusive leases |
| [`product/project-intelligence.md`](product/project-intelligence.md) | Instructions/skills discovery |
| [`product/filesystem-readonly.md`](product/filesystem-readonly.md) | `fs_*` read path |
| [`product/filesystem-apply-patch.md`](product/filesystem-apply-patch.md) | `fs_apply_patch` |
| [`product/filesystem-binary-write.md`](product/filesystem-binary-write.md) | `fs_write_binary` + `fs_write_file` |
| [`product/exec-run.md`](product/exec-run.md) | Deterministic exec |
| [`product/terminals.md`](product/terminals.md) | tmux lifecycle |
| [`product/browser.md`](product/browser.md) | Playwright loopback |
| [`product/visual.md`](product/visual.md) | Screenshots + diff |
| [`product/ops.md`](product/ops.md) | `ops_status` / `ops_cleanup`, quotas |
| [`product/service-control.md`](product/service-control.md) | Restart your app's systemd unit from the chat (optional) |

## Architecture

| Page | Purpose |
|------|---------|
| [`architecture/layers.md`](architecture/layers.md) | Core vs remote adapter vs targets |
| [`architecture/first-install.md`](architecture/first-install.md) | Greenfield server install |
| [`architecture/host-requirements.md`](architecture/host-requirements.md) | Generic host requirements |
| [`architecture/remote-mcp-transport.md`](architecture/remote-mcp-transport.md) | Remote MCP over HTTPS/FastMCP/OAuth |
| [`architecture/hardening.md`](architecture/hardening.md) | Unix/systemd/perimeter |
| [`architecture/chatgpt-file-params.md`](architecture/chatgpt-file-params.md) | ChatGPT `openai/fileParams` → Core write |
