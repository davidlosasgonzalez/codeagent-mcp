# Supported MCP clients

CodeAgent MCP implements the Model Context Protocol, so any compliant client can drive it.
What differs is *how* each client reaches the server.

## Two ways in

| Transport | Who it is for |
|-----------|---------------|
| **Remote HTTPS** (Streamable HTTP + GitHub OAuth) | Hosted chat clients. This is the production path and what the tool descriptions are tuned for. Needs a public hostname, TLS, and an OAuth app — see [`../architecture/first-install.md`](../architecture/first-install.md). |
| **Stdio** | A client running on the same machine as the server. Useful for development and for inspecting the tool catalog; no domain, proxy, or OAuth required. |

## Clients that accept a remote MCP server

Support and plan requirements move quickly; confirm against each vendor's current
documentation before you commit to one.

| Client | Availability | Notes |
|--------|--------------|-------|
| **ChatGPT** | Plus, Pro, Business, Enterprise, Edu — web app, Developer Mode | The reference client, and the only one verified end to end against this server. Guide: [`connect-chatgpt.md`](connect-chatgpt.md). |
| **Claude** (claude.ai web and desktop) | All plans, via custom connectors | Distinct from Claude Code, which is a local coding agent — see below. |
| **Perplexity** | Pro, Max, Enterprise | |
| **Mistral Le Chat** | All plans | |
| **Grok** | Paid accounts | |
| **Gemini** | Gemini API and Gemini Enterprise | In the consumer Gemini app, custom MCP connectors are limited to users with Spark access and only inside Spark tasks. |

Any of these needs the endpoint to be a public HTTPS URL. None of them can reach a server
on your laptop or inside a private network.

## A note on local coding agents

Claude Code, Cursor, and similar tools can connect over stdio, but there is little reason
to: they already ship their own filesystem, command execution, and terminal tooling, so
CodeAgent would duplicate what they do natively.

The value of this project is giving a **hosted chat assistant** — which has no filesystem
and no shell of its own — a real, bounded development environment on a machine you control.
Use stdio for development of CodeAgent itself, not as the end goal.
