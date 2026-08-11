"""ChatGPT openai/fileParams download adapter (adaptation layer, not Core).

Resolves temporary download_url references with a narrow HTTPS client:
host allowlist, no redirects, DNS pin + private-IP deny, stream size cap.
Never logs full URLs (query strings are capabilities).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from codeagent_mcp.errors import tool_error
from codeagent_mcp.fs.binary_write import MAX_BINARY_WRITE_BYTES

logger = logging.getLogger(__name__)

# Default suffixes for ChatGPT/OpenAI temporary file hosts (override via env).
# Only domains OpenAI owns outright belong here: every subdomain is trusted.
_DEFAULT_HOST_SUFFIXES = (
    "oaiusercontent.com",
    "openai.com",
    "chatgpt.com",
)

# Multi-tenant storage domains: anyone can own a name under them, so the bare
# suffix is never allowlisted. Only the leftmost label decides. ChatGPT's
# code-interpreter sandbox — where /mnt/data and ImageGen output live — is
# served from OpenAI storage accounts named oaisdmnt<region>, e.g.
# oaisdmntprnortheu.blob.core.windows.net.
_MULTI_TENANT_SUFFIXES = (
    "blob.core.windows.net",
    "s3.amazonaws.com",
    "storage.googleapis.com",
    "r2.cloudflarestorage.com",
)
_DEFAULT_ACCOUNT_PREFIXES = ("oaisdmnt",)

CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 60.0
MAX_HEADER_BYTES = 64_000


@dataclass(frozen=True)
class OpenAIFileRef:
    """Wire shape for openai/fileParams (adaptation only)."""

    download_url: str
    file_id: str
    mime_type: str = ""
    file_name: str = ""


def _suffix_is_safe(suffix: str) -> bool:
    """Reject suffixes too broad to be an ownership claim (fail closed on config)."""
    if suffix in _MULTI_TENANT_SUFFIXES:
        return False
    if any(suffix.endswith("." + shared) for shared in _MULTI_TENANT_SUFFIXES):
        # e.g. "someaccount.blob.core.windows.net" — express it as a prefix instead,
        # so a sibling account under the same domain cannot be reached.
        return False
    # A bare TLD ("com") or a public suffix would trust the whole internet.
    return suffix.count(".") >= 1


def host_suffix_allowlist() -> tuple[str, ...]:
    """Host suffixes allowed for temporary ChatGPT/OpenAI download URLs.

    Override with ``CODEAGENT_FILE_DOWNLOAD_HOST_SUFFIXES``. Entries that would
    trust a multi-tenant storage domain are dropped, not honoured.
    """
    raw = os.environ.get("CODEAGENT_FILE_DOWNLOAD_HOST_SUFFIXES", "").strip()
    if not raw:
        return _DEFAULT_HOST_SUFFIXES
    parts = []
    for item in raw.split(","):
        suffix = item.strip().lower().lstrip(".")
        if not suffix:
            continue
        if not _suffix_is_safe(suffix):
            logger.warning(
                "ignoring unsafe file-download host suffix %r "
                "(multi-tenant or public suffix); use account prefixes instead",
                suffix,
            )
            continue
        parts.append(suffix)
    return tuple(parts) or _DEFAULT_HOST_SUFFIXES


def account_prefix_allowlist() -> tuple[str, ...]:
    """Leftmost-label prefixes allowed under multi-tenant storage domains.

    Override with ``CODEAGENT_FILE_DOWNLOAD_ACCOUNT_PREFIXES`` when OpenAI
    changes its sandbox storage naming. Empty value disables the rule entirely.
    """
    raw = os.environ.get("CODEAGENT_FILE_DOWNLOAD_ACCOUNT_PREFIXES")
    if raw is None:
        return _DEFAULT_ACCOUNT_PREFIXES
    parts = []
    for item in raw.split(","):
        prefix = item.strip().lower()
        if not prefix:
            continue
        if len(prefix) < 3:
            logger.warning(
                "ignoring file-download account prefix %r (too short to identify an owner)",
                prefix,
            )
            continue
        parts.append(prefix)
    return tuple(parts)


def parse_openai_file_ref(file: Any) -> OpenAIFileRef | dict[str, Any]:
    """Validate a ChatGPT ``openai/fileParams`` object.

    Returns :class:`OpenAIFileRef` on success, or a ``tool_error`` dict.
    """
    if not isinstance(file, dict):
        # Pydantic model support
        if hasattr(file, "model_dump"):
            file = file.model_dump()
        else:
            return tool_error(
                "INVALID_ARGUMENT",
                "file must be an object with download_url and file_id",
                retryable=False,
            )
    if not isinstance(file, dict):
        return tool_error(
            "INVALID_ARGUMENT",
            "file must be an object with download_url and file_id",
            retryable=False,
        )
    extra = set(file.keys()) - {"download_url", "file_id", "mime_type", "file_name"}
    if extra:
        return tool_error(
            "INVALID_ARGUMENT",
            "file has unsupported fields",
            retryable=False,
        )
    download_url = file.get("download_url")
    file_id = file.get("file_id")
    if not isinstance(download_url, str) or not download_url.strip():
        return tool_error(
            "INVALID_ARGUMENT",
            "file.download_url is required",
            retryable=False,
        )
    if not isinstance(file_id, str) or not file_id.strip():
        return tool_error(
            "INVALID_ARGUMENT",
            "file.file_id is required",
            retryable=False,
        )
    mime_type = file.get("mime_type", "")
    file_name = file.get("file_name", "")
    if mime_type is None:
        mime_type = ""
    if file_name is None:
        file_name = ""
    if not isinstance(mime_type, str) or not isinstance(file_name, str):
        return tool_error(
            "INVALID_ARGUMENT",
            "file.mime_type and file.file_name must be strings when present",
            retryable=False,
        )
    return OpenAIFileRef(
        download_url=download_url.strip(),
        file_id=file_id.strip(),
        mime_type=mime_type,
        file_name=file_name,
    )


def _host_allowed(hostname: str, suffixes: tuple[str, ...]) -> bool:
    host = hostname.lower().rstrip(".")
    for suffix in suffixes:
        if host == suffix or host.endswith("." + suffix):
            return True
    return _account_host_allowed(host, account_prefix_allowlist())


def _account_host_allowed(host: str, prefixes: tuple[str, ...]) -> bool:
    """Allow a single storage account under a multi-tenant domain, by label prefix.

    ``host`` must be exactly ``<account>.<multi-tenant suffix>``: one label, so a
    nested attacker-controlled subdomain cannot inherit the account's trust.
    """
    if not prefixes:
        return False
    for shared in _MULTI_TENANT_SUFFIXES:
        if not host.endswith("." + shared):
            continue
        account = host[: -(len(shared) + 1)]
        if "." in account:
            return False
        return any(account.startswith(prefix) for prefix in prefixes)
    return False


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _safe_host_label(hostname: str) -> str:
    """Host only — never include URL query/path (signed URLs are capabilities)."""
    return hostname.lower().rstrip(".")[:200]


def validate_download_url(url: str) -> tuple[str, str, int] | dict[str, Any]:
    """Validate ``download_url``; return ``(hostname, path_query, port)`` or tool_error.

    Never echoes the full URL (query strings are capabilities).
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return tool_error(
            "INVALID_ARGUMENT",
            "file.download_url is not a valid URL",
            retryable=False,
        )
    if parsed.scheme.lower() != "https":
        return tool_error(
            "RISK_BLOCKED",
            "file download requires https",
            retryable=False,
        )
    if parsed.username or parsed.password:
        return tool_error(
            "RISK_BLOCKED",
            "file download URL must not include credentials",
            retryable=False,
        )
    hostname = parsed.hostname
    if not hostname:
        return tool_error(
            "INVALID_ARGUMENT",
            "file.download_url missing host",
            retryable=False,
        )
    if not _host_allowed(hostname, host_suffix_allowlist()):
        return tool_error(
            "RISK_BLOCKED",
            f"file download host not allowlisted ({_safe_host_label(hostname)})",
            retryable=False,
            next_action=(
                "Use fs_write_binary with plain Base64, or — if the host really is "
                "OpenAI-owned — widen CODEAGENT_FILE_DOWNLOAD_HOST_SUFFIXES / "
                "CODEAGENT_FILE_DOWNLOAD_ACCOUNT_PREFIXES "
                "(docs/architecture/chatgpt-file-params.md)"
            ),
        )
    port = parsed.port or 443
    if port != 443:
        # Keep surface small; OpenAI temps use 443.
        return tool_error(
            "RISK_BLOCKED",
            "file download must use port 443",
            retryable=False,
        )
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return hostname, path, port


def _resolve_public_ips(hostname: str) -> list[str] | dict[str, Any]:
    try:
        infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return tool_error(
            "INVALID_ARGUMENT",
            f"cannot resolve file download host ({_safe_host_label(hostname)})",
            retryable=True,
        )
    addrs: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip_s = str(sockaddr[0])
        try:
            ip = ipaddress.ip_address(ip_s)
        except ValueError:
            continue
        if _ip_blocked(ip):
            return tool_error(
                "RISK_BLOCKED",
                f"file download resolved to a blocked address family "
                f"({_safe_host_label(hostname)})",
                retryable=False,
            )
        if ip_s not in addrs:
            addrs.append(ip_s)
    if not addrs:
        return tool_error(
            "INVALID_ARGUMENT",
            f"no usable addresses for file download host ({_safe_host_label(hostname)})",
            retryable=True,
        )
    return addrs


def download_openai_file_bytes(
    ref: OpenAIFileRef,
    *,
    max_bytes: int = MAX_BINARY_WRITE_BYTES,
) -> bytes | dict[str, Any]:
    """Fetch file bytes over HTTPS (allowlist, no redirects, DNS pin, size cap).

    Returns raw bytes on success, or a ``tool_error`` dict.
    """
    _ = ref.file_id  # identity metadata only; not used for authz
    validated = validate_download_url(ref.download_url)
    if isinstance(validated, dict):
        return validated
    hostname, path_query, port = validated
    addrs = _resolve_public_ips(hostname)
    if isinstance(addrs, dict):
        return addrs

    last_err = "download failed"
    for ip_s in addrs:
        try:
            return _https_get_pinned(
                hostname=hostname,
                path_query=path_query,
                port=port,
                ip=ip_s,
                max_bytes=max_bytes,
            )
        except _DownloadError as exc:
            last_err = exc.message
            if exc.fatal:
                return tool_error(exc.code, exc.message, retryable=exc.retryable)
            continue
    return tool_error("INTERNAL_ERROR", last_err, retryable=True)


class _DownloadError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        fatal: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.fatal = fatal


def _https_get_pinned(
    *,
    hostname: str,
    path_query: str,
    port: int,
    ip: str,
    max_bytes: int,
) -> bytes:
    """Connect to pre-validated IP with SNI=hostname; reject redirects; stream-cap."""
    # Re-check IP immediately before connect (anti-rebind / stale resolve).
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError as exc:
        raise _DownloadError("RISK_BLOCKED", "invalid resolved address", fatal=True) from exc
    if _ip_blocked(ip_obj):
        raise _DownloadError(
            "RISK_BLOCKED",
            f"file download address blocked ({_safe_host_label(hostname)})",
            fatal=True,
        )

    family = socket.AF_INET6 if ip_obj.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT_S)
    tls: ssl.SSLSocket | None = None
    try:
        sock.connect((ip, port))
        ctx = ssl.create_default_context()
        tls = ctx.wrap_socket(sock, server_hostname=hostname)
        tls.settimeout(READ_TIMEOUT_S)
        req = (
            f"GET {path_query} HTTP/1.1\r\n"
            f"Host: {hostname}\r\n"
            "Connection: close\r\n"
            "User-Agent: codeagent-mcp-file-adapter/1\r\n"
            "Accept: */*\r\n"
            "\r\n"
        ).encode("ascii")
        tls.sendall(req)
        return _read_http_body(tls, max_bytes=max_bytes, hostname=hostname)
    except TimeoutError as exc:
        raise _DownloadError(
            "INTERNAL_ERROR",
            f"file download timed out ({_safe_host_label(hostname)})",
            retryable=True,
        ) from exc
    except OSError as exc:
        raise _DownloadError(
            "INTERNAL_ERROR",
            f"file download connection failed ({_safe_host_label(hostname)})",
            retryable=True,
        ) from exc
    finally:
        if tls is not None:
            try:
                tls.close()
            except OSError:
                pass
        else:
            try:
                sock.close()
            except OSError:
                pass


def _read_http_body(tls: ssl.SSLSocket, *, max_bytes: int, hostname: str) -> bytes:
    buf = bytearray()
    while b"\r\n\r\n" not in buf and len(buf) < MAX_HEADER_BYTES:
        chunk = tls.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
    if b"\r\n\r\n" not in buf:
        raise _DownloadError(
            "INTERNAL_ERROR",
            f"file download returned incomplete headers ({_safe_host_label(hostname)})",
            retryable=True,
            fatal=True,
        )
    header_blob, rest = bytes(buf).split(b"\r\n\r\n", 1)
    try:
        header_text = header_blob.decode("iso-8859-1")
    except UnicodeDecodeError as exc:
        raise _DownloadError(
            "INTERNAL_ERROR",
            f"file download headers invalid ({_safe_host_label(hostname)})",
            fatal=True,
        ) from exc
    lines = header_text.split("\r\n")
    if not lines:
        raise _DownloadError(
            "INTERNAL_ERROR",
            f"file download empty status ({_safe_host_label(hostname)})",
            fatal=True,
        )
    status_parts = lines[0].split(" ", 2)
    if len(status_parts) < 2 or not status_parts[1].isdigit():
        raise _DownloadError(
            "INTERNAL_ERROR",
            f"file download bad status line ({_safe_host_label(hostname)})",
            fatal=True,
        )
    status = int(status_parts[1])
    if 300 <= status < 400:
        raise _DownloadError(
            "RISK_BLOCKED",
            f"file download redirects are not allowed ({_safe_host_label(hostname)})",
            fatal=True,
        )
    if status != 200:
        raise _DownloadError(
            "INTERNAL_ERROR",
            f"file download HTTP {status} ({_safe_host_label(hostname)})",
            retryable=status >= 500,
            fatal=True,
        )

    headers = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        headers[k.strip().lower()] = v.strip()
    if headers.get("transfer-encoding", "").lower() == "chunked":
        raise _DownloadError(
            "INVALID_ARGUMENT",
            f"chunked file downloads are not supported ({_safe_host_label(hostname)})",
            fatal=True,
        )
    cl = headers.get("content-length")
    if cl is not None:
        try:
            declared = int(cl)
        except ValueError as exc:
            raise _DownloadError(
                "INVALID_ARGUMENT",
                f"invalid Content-Length ({_safe_host_label(hostname)})",
                fatal=True,
            ) from exc
        if declared > max_bytes:
            raise _DownloadError(
                "INVALID_ARGUMENT",
                f"file exceeds {max_bytes} bytes (Content-Length)",
                fatal=True,
            )

    body = bytearray(rest)
    while len(body) < max_bytes + 1:
        chunk = tls.recv(64 * 1024)
        if not chunk:
            break
        body.extend(chunk)
        if len(body) > max_bytes:
            raise _DownloadError(
                "INVALID_ARGUMENT",
                f"file exceeds {max_bytes} bytes",
                fatal=True,
            )
    return bytes(body)
