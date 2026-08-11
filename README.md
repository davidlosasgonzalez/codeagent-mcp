# CodeAgent MCP

[![CI](https://github.com/davidlosasgonzalez/codeagent-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/davidlosasgonzalez/codeagent-mcp/actions/workflows/ci.yml)
[![Secret scan](https://github.com/davidlosasgonzalez/codeagent-mcp/actions/workflows/gitleaks.yml/badge.svg)](https://github.com/davidlosasgonzalez/codeagent-mcp/actions/workflows/gitleaks.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/release/python-3120/)
[![MCP](https://img.shields.io/badge/MCP-Streamable%20HTTP-8A2BE2.svg)](https://modelcontextprotocol.io)

**Give a hosted AI assistant a real development environment on a server you control.**

CodeAgent MCP is a self-hosted server that lets ChatGPT — or any other MCP client — work on
code that lives on your own Linux machine: read and edit files, run commands, drive
persistent tmux terminals, and inspect a running app through browser screenshots. It speaks
the [Model Context Protocol](https://modelcontextprotocol.io), the open standard that AI
assistants use to reach external tools.

A chat assistant has no filesystem and no shell. This gives it both, so it can develop and
operate a project that lives on a remote server the way a local coding agent works on your
laptop — from a normal conversation, with no SSH session of your own.

In practice: you ask it to fix a failing test in your staging app. It reads the file,
applies a patch, runs the suite in a terminal that stays alive between messages, opens the
page in a headless browser, and shows you a screenshot of the result — all on your machine,
never leaving the project roots you allowed.

Access is bounded by a project registry you define, an exclusive lease per writer, and
per-project write gates that are off by default.

## Works with

| Client | How it connects |
|--------|-----------------|
| **ChatGPT** (Plus, Pro, Business, Enterprise, Edu) | Remote HTTPS — the reference client, verified end to end. **[Setup guide →](docs/product/connect-chatgpt.md)** |
| **Claude** (claude.ai), **Perplexity**, **Mistral Le Chat**, **Grok** | Remote HTTPS, via each client's custom connector |
| **Gemini** | Gemini API and Gemini Enterprise; limited in the consumer app |
| Local MCP clients (Cursor, Claude Code…) | Stdio — works, but they already ship their own file and shell tools |

Details and plan requirements: [`docs/product/clients.md`](docs/product/clients.md).

## What it can do

39 tools, grouped by what they touch:

- **Workspace:** `workspace_acquire` · `workspace_status` · `workspace_release`
- **Filesystem:** `fs_stat` · `fs_list` · `fs_read` · `fs_search` · `fs_apply_patch` · `fs_write_binary` · `fs_write_file`
- **Project intelligence:** `project_bootstrap` · `project_instructions` · `project_skills_list` · `project_skill_read`
- **Git:** `git_status` · `git_diff`
- **Exec:** `exec_run`
- **Terminal:** `terminal_list` · `terminal_status` · `terminal_create` · `terminal_write` · `terminal_key` · `terminal_read` · `terminal_snapshot` · `terminal_interrupt` · `terminal_close` · `terminal_reset`
- **Browser/visual:** `browser_ensure` · `browser_set_viewport` · `browser_reload` · `browser_open` · `browser_action` · `browser_snapshot` · `visual_capture` · `visual_get` · `visual_compare`
- **Ops:** `ops_status` · `ops_cleanup`
- **Meta:** `server_info`

Two of those are easy to overlook:

- **`fs_write_file` moves files off the chat and into the repo.** Attach anything to the
  conversation — a mockup, a PDF spec, a CSV fixture, a font — or have the assistant generate an
  image, and it lands in your checkout. Any type, up to 2 MB, travelling host-to-host rather than
  through the prompt, so nothing is truncated or re-encoded. See
  [`filesystem-binary-write.md`](docs/product/filesystem-binary-write.md).
- **`visual_capture` + `visual_compare` close the loop on work you cannot see.** Screenshot before
  and after a change and diff the pixels. See
  [`frontend-workflow.md`](docs/product/frontend-workflow.md).

Full reference: [`docs/product/tool-surface.md`](docs/product/tool-surface.md).

## How access is bounded

- **The client never chooses a path.** Every reachable checkout is declared server-side in
  [`projects.yaml`](docs/product/projects-registry.md); clients pass only a project id.
- **Writes are off until you enable them,** per project, through a `writable_env` gate plus
  systemd `ReadWritePaths=`.
- **Every mutating tool needs an exclusive lease,** so two sessions cannot edit the same
  checkout at once.
- **The process runs as a restricted system user** with no sudo and no Docker access,
  bound to loopback behind a TLS reverse proxy, with a GitHub OAuth subject allowlist.

## Requirements

**Linux only, on x86-64 or arm64.** Path confinement is built on the `openat2` syscall, so
the kernel must be **5.6 or newer** — check with `uname -r`. Current distributions are
fine; the common trap is Ubuntu 20.04, whose stock 5.4 kernel is too old. macOS and Windows
are not supported, not even for running the test suite.

You also need Python 3.12, [`uv`](https://docs.astral.sh/uv/), and `git`, `tmux` and
`ripgrep` on the host. The remote path adds a TLS reverse proxy and systemd. Full list:
[`docs/architecture/host-requirements.md`](docs/architecture/host-requirements.md).

All of this applies to the **machine that runs the server**. Your own computer can be
anything: you reach it through a chat client in the browser, so Windows and macOS are fine
on your side.

## Install on a server

The ordered greenfield checklist is
[`docs/architecture/first-install.md`](docs/architecture/first-install.md): system user →
`uv` and Python 3.12 → clone and `uv sync` → project registry → DNS → `http.env` and GitHub
OAuth → systemd → reverse proxy → connector → verify.

| Template | Purpose |
|----------|---------|
| [`deploy/http.env.example`](deploy/http.env.example) | Secrets and env → `/etc/codeagent-mcp/http.env` |
| [`deploy/projects.example.yaml`](deploy/projects.example.yaml) | Project registry → `/etc/codeagent-mcp/projects.yaml` |
| [`deploy/codeagent-mcp-http.service`](deploy/codeagent-mcp-http.service) | systemd unit (edit `ReadWritePaths` and UID) |
| [`deploy/Caddyfile.example`](deploy/Caddyfile.example) | TLS reverse proxy → loopback `:8765` |

A domain is required only for this remote path, because TLS, the OAuth callback, and the
hosted clients all expect a hostname rather than a bare IP.

## Run it locally

Still a Linux box — see [Requirements](#requirements). Useful for development and for
inspecting the tool catalog; the remote path above is the real deployment.

```bash
git clone https://github.com/davidlosasgonzalez/codeagent-mcp.git
cd codeagent-mcp
uv sync
uv run codeagent-mcp
```

That serves MCP over stdio. Project roots still come from the registry: copy
[`deploy/projects.example.yaml`](deploy/projects.example.yaml) and point
`CODEAGENT_PROJECTS_FILE` at it.

## Security

This server executes commands and edits files on the host that runs it. Read
[`docs/architecture/hardening.md`](docs/architecture/hardening.md) before exposing it, and
[`SECURITY.md`](SECURITY.md) for the security model and how to report a vulnerability.

## Documentation

Everything is indexed in [`docs/README.md`](docs/README.md).

## License

[MIT](LICENSE) © 2026 David Losas González
