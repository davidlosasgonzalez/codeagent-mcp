# Filesystem binary / file write

Tools: `fs_write_binary` (portable Base64) · `fs_write_file` (ChatGPT `openai/fileParams`).

## Purpose

Move a file that exists on the **client** side into a project root on the server.

There is no allowlist of types or extensions: any file up to **2 MB** is accepted — a design
mockup, a PDF spec, a CSV fixture, a font, a logo, a certificate. What is constrained is where
the bytes may come from (`fs_write_file` fetches only from allowlisted hosts) and where they may
land (lease, PathJail, write gate), never what they contain.

For a ChatGPT attachment or an ImageGen result, prefer `fs_write_file`: the bytes travel
host-to-host and never pass through the prompt, so nothing can be truncated or re-encoded on the
way. Reach for `fs_write_binary` only when the client cannot fill `fileParams`.

### The traffic is one-way

Inbound and outbound are **not** symmetric, and it is worth knowing before you plan around it:

| Direction | What can move |
|-----------|---------------|
| Client → project root | Any file, any type, up to 2 MB (`fs_write_file` / `fs_write_binary`) |
| Project root → client | UTF-8 text (`fs_read`) and screenshots of a rendered page (`visual_capture`) |

`fs_read` refuses binaries by design — it answers `UNSUPPORTED_BINARY` with the file's `sha256`
so you can still drive a replace flow. Nothing hands an arbitrary binary in the checkout back to
the client: a PDF, a font or an archive that lives on the server stays there. That tool does not
exist, and the surface is only extended in response to observed friction.

If you genuinely need a small binary out, the escape hatch is `exec_run` with `base64`, bounded
by the 200 KB stdout cap (~150 KB of file) and paid for in prompt tokens. Treat it as a
workaround, not a feature.

| Tool | Client path | Core |
|------|-------------|------|
| `fs_write_binary` | Plain Base64 argument | `write_bytes` |
| `fs_write_file` | ChatGPT file ref → adaptation HTTPS GET | `write_bytes` |

Shared Core invariants: lease, PathJail, SHA/`create`, **2_000_000** byte cap, write gates, atomic temp+fsync+replace. See [`../architecture/chatgpt-file-params.md`](../architecture/chatgpt-file-params.md).

## Contract (both tools)

- **Requires** `lease_id` (mutating). Effective project is always `lease["project"]`.
- Existing files: `expected_sha256` mandatory. For binaries, `fs_read` returns `UNSUPPORTED_BINARY` **with** `sha256`. Mismatch → `CONFLICT`.
- `create=true`: new file only; `expected_sha256` must be empty. Mode `0o644`. Existing file keeps its mode.
- Parent directory must already exist (no auto-mkdir).
- Write gate: same as `fs_apply_patch` (`ProjectConfig.writable` / `CODEAGENT_*_WRITE`).

### `fs_write_binary`

- Input: plain Base64 in `content_base64` (FastMCP `str`). **Not** a data URL. **No** remote fetch.
- Encoding shape is normalised before decoding: line breaks / whitespace are stripped, the URL-safe alphabet (`-` `_`) is accepted, and missing `=` padding is restored. Agents wrap long Base64 across lines; rejecting that turned one write into several retries. Size caps and SHA gates are unchanged — only the alphabet is forgiving, never the byte count.
- A **truncated** payload is not recoverable here: it decodes to fewer bytes. Verify `size_bytes`/`sha256` in the response, or prefer `fs_write_file`.
- Annotation: **DEST** (`openWorldHint=false`).

### `fs_write_file` (ChatGPT adaptation)

- Input: `file` object with `download_url` + `file_id` (required), `mime_type` + `file_name` (optional strings).
- Tool `_meta`: `{"openai/fileParams": ["file"]}`.
- Preferred path for a ChatGPT-side file (upload or ImageGen output): the bytes never pass through the prompt, so nothing can be truncated or re-encoded on the way.
- Downloads **HTTPS only** from allowlisted hosts — OpenAI-owned domains plus the sandbox storage account that backs `/mnt/data`. See [`../architecture/chatgpt-file-params.md`](../architecture/chatgpt-file-params.md#download-host-rules).
- **No redirects**; DNS resolve + private/link-local/metadata IP deny; stream capped at 2_000_000 (do not trust Content-Length alone).
- `path` is authoritative — never `upload_dir / file_name`.
- Never logs full `download_url` (signed query = capability).
- Annotation: **DEST_OPEN** (`openWorldHint=true`).
- ImageGen → fileParams works through the same reference; the host that serves those bytes is **observed, not promised** by OpenAI (see architecture doc).

## Response (success)

Small metadata only: `ok`, `project`, `path`, `relative`, `sha256`, `size_bytes`, `created`. Never echoes Base64, bytes, or download URLs.

## Errors

| Code | When |
|------|------|
| `LEASE_REQUIRED` | Missing `lease_id` |
| `WRITE_DISABLED` | Project not writable |
| `INVALID_ARGUMENT` | Bad Base64 / data URL / oversized / missing hash / bad file object |
| `RISK_BLOCKED` | Non-allowlisted host, http, credentials, redirects, private IP |
| `CONFLICT` | Stale `expected_sha256` |
| `PATH_OUTSIDE_ROOT` | Jail escape |
| `NOT_FOUND` | Missing file without `create=true` |
| `PERMISSION_DENIED` | EACCES/EROFS |

## Example (conceptual)

**Portable:** Base64 → `fs_write_binary`.

**ChatGPT:** attachment/ImageGen (if host fills fileParams) → `fs_write_file(path=…, lease_id=…, file={…}, create=true)` → optional `fs_apply_patch` for references → browser/visual verify.

## Deliberate non-goals

- OpenAI Image API inside the server.
- Generic `fs_write_url` / arbitrary URL fetch.
- OpenAI types or HTTP download inside Core.
- Raising Caddy body limit alone.
- Caching blobs or signed URLs.
