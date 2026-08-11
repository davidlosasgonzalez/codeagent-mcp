# Architecture layers

```text
                 CodeAgent MCP Core (shared tools)
                           │
                ┌──────────┼──────────┐
                │          │          │
             ChatGPT    Claude     Other MCP clients
                │
     HTTPS + FastMCP Streamable HTTP + OAuth
              (production remote path)
                │
           stdio (local/dev)
```

- **Core:** tools, leases, path roots, exec, tmux/spool, Playwright, project intelligence, ops, errors — MCP standard. Binary ingest Core path: Base64 (`fs_write_binary`) and `write_bytes` (used by `fs_write_file`); **no** OpenAI file IDs/URLs in Core.
- **ChatGPT-first adaptation:** naming/descriptions, limits, ImageContent, ChatGPT benchmarks; **`openai/fileParams`** download adapter for `fs_write_file` — see [`chatgpt-file-params.md`](chatgpt-file-params.md).
- **Targets:** checkouts declared only in server-side `projects.yaml` (see [`../product/projects-registry.md`](../product/projects-registry.md)). The server identity is CodeAgent MCP, not any particular app.

## Core tool groups

Workspace · Filesystem · Project intelligence · `exec_run` · Terminal · Browser/visual · Ops (`ops_status` / `ops_cleanup`) · `server_info`.

Canonical list: [`../product/tool-surface.md`](../product/tool-surface.md) (**39** tools).

Skills are discovered and delivered; the LLM client orchestrates; Core primitives execute. There is deliberately no `project_skill_run` tool.
