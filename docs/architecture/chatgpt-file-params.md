# ChatGPT fileParams → project write (adaptation)

Product page: [`../product/filesystem-binary-write.md`](../product/filesystem-binary-write.md).

## Verdict

The documented ChatGPT path to pass a file into an MCP tool **without Base64 in the prompt** is:

```json
"_meta": { "openai/fileParams": ["file"] }
```

ChatGPT fills the `file` parameter with a **reference** (`file_id` + temporary `download_url`). The `tools/call` is normal JSON-RPC; bytes arrive via a narrow **HTTPS GET** in the adaptation layer.

| Claim | Status |
|-------|--------|
| No Base64 in prompt/tool call | Yes |
| No HTTP download at all | **No** |
| Pure portable MCP | **No** — `openai/...` extension |

## Layers (implemented)

```text
adaptation/chatgpt_file_download.py     Core fs/binary_write.write_bytes
openai/fileParams validate              size 2_000_000, SHA, lease,
HTTPS allowlist + DNS pin + no redirect PathJail, atomic commit
        │                                         ▲
        └────────────── bytes ────────────────────┘

fs_write_binary (Base64) ───────────────┘
```

Code: `src/codeagent_mcp/adaptation/chatgpt_file_download.py`, tool `fs_write_file` in `tools/fs.py`.

## Download host rules

`download_url` is a **temporary** URL. OpenAI does not document which hosts serve it, so the allowlist encodes what has been *observed*, in two deliberately different shapes.

| Rule | Value | Why this shape |
|------|-------|----------------|
| Suffix | `oaiusercontent.com`, `openai.com`, `chatgpt.com` | OpenAI owns the whole domain, so every subdomain is theirs. Env: `CODEAGENT_FILE_DOWNLOAD_HOST_SUFFIXES` |
| Account prefix | leftmost label starts with `oaisdmnt`, directly under `blob.core.windows.net` | The code-interpreter sandbox (`/mnt/data`, ImageGen output) is served from OpenAI's own Azure storage accounts, e.g. `oaisdmntprnortheu.blob.core.windows.net`. Env: `CODEAGENT_FILE_DOWNLOAD_ACCOUNT_PREFIXES` |

**`blob.core.windows.net` is never allowlisted as a suffix.** It is multi-tenant: anyone can rent a name under it, so trusting the suffix would turn `fs_write_file` into a general-purpose downloader. Only the account label decides, and it must be a single label — `oaisdmntprnortheu.attacker.blob.core.windows.net` is blocked.

The same protection applies to configuration: an env value naming a multi-tenant domain (`blob.core.windows.net`, `s3.amazonaws.com`, `storage.googleapis.com`, `r2.cloudflarestorage.com`), a single-account host under one, or a bare public suffix is **dropped with a warning**, not honoured.

### Residual risk, stated plainly

Azure storage account names are globally unique and first-come. Nothing proves cryptographically that `oaisdmnt*` belongs to OpenAI; the prefix is a strong but unofficial signal. Accepted because the remaining locks stand on their own — the write still needs an active lease and a writable project, stays inside PathJail at a caller-chosen `path`, is capped at 2MB, and follows no redirects. Set `CODEAGENT_FILE_DOWNLOAD_ACCOUNT_PREFIXES=""` to drop the rule and fall back to `fs_write_binary`.

### What is external, and cannot be fixed here

- OpenAI publishes no contract for the download host. If the sandbox storage is renamed, the prefix env is the one-line fix; no release is needed.
- `file_id` is **identity metadata only**. It is not an API Files object and this server holds no OpenAI credentials, so it cannot be exchanged for bytes — the temporary URL is the only usable handle. Authorization comes from the lease, never from the reference.
- Whether ChatGPT fills `fileParams` at all for a given attachment is a client-side decision; when it does not, `fs_write_binary` is the fallback.

## Security locks

| Lock | Implementation |
|------|----------------|
| Host allowlist | Owned-domain suffixes + single-account prefix rule (see above); multi-tenant suffixes rejected even from env |
| Redirects | HTTP 3xx → `RISK_BLOCKED` (no follow) |
| DNS / IP | `getaddrinfo` + deny private/loopback/link-local/multicast/reserved / IPv4-mapped |
| Timeouts | connect 10s, read 60s |
| URL secrecy | errors only include hostname label; never query string |
| Schema | Pydantic `OpenAIFileParam` — four string fields; `mime_type`/`file_name` default `""` (not null) |
| Annotations | `DEST_OPEN` (`openWorldHint=true`) |

## ImageGen

Attachments and ImageGen output both arrive as the same reference. The difference is *where the bytes sit*: ImageGen writes into the code-interpreter sandbox, so its `download_url` points at the sandbox storage account rather than at `oaiusercontent.com`. That single fact is what made the first attempt fail with `RISK_BLOCKED`, and it is why the account-prefix rule exists.

Two dead ends worth not re-walking:

- `/mnt/data` is **not** a shared filesystem. It lives in ChatGPT's sandbox, not on the MCP host, so `os.path.exists('/mnt/data/…')` is `False` here and `cp` cannot reach it. The reference is the only bridge.
- Hand-copying Base64 as a workaround invites truncation and re-encoding, and the pressure to shrink the payload degrades the asset. `fs_write_binary` now tolerates wrapped/URL-safe Base64, but `fs_write_file` remains the right tool for a file that already exists on the ChatGPT side.

## Sources

- [OpenAI Plugins reference — fileParams](https://developers.openai.com/plugins/reference)
