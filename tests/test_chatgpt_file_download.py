"""Tests for ChatGPT fileParams download adapter (SSRF / allowlist)."""

from __future__ import annotations

import ipaddress
from typing import Any

import pytest

from codeagent_mcp.adaptation import chatgpt_file_download as dl


def test_parse_openai_file_ref_ok() -> None:
    ref = dl.parse_openai_file_ref(
        {
            "download_url": "https://files.oaiusercontent.com/x?sig=1",
            "file_id": "file_abc",
            "mime_type": "image/png",
            "file_name": "a.png",
        }
    )
    assert isinstance(ref, dl.OpenAIFileRef)
    assert ref.file_id == "file_abc"
    assert ref.mime_type == "image/png"


def test_parse_rejects_extra_fields() -> None:
    err = dl.parse_openai_file_ref(
        {
            "download_url": "https://files.oaiusercontent.com/x",
            "file_id": "file_abc",
            "evil": "1",
        }
    )
    assert isinstance(err, dict)
    assert err["ok"] is False


def test_validate_rejects_http_and_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dl, "host_suffix_allowlist", lambda: ("oaiusercontent.com",))
    err = dl.validate_download_url("http://files.oaiusercontent.com/x")
    assert isinstance(err, dict)
    assert err["error"]["code"] == "RISK_BLOCKED"

    err2 = dl.validate_download_url("https://127.0.0.1/x")
    assert isinstance(err2, dict)
    assert err2["error"]["code"] == "RISK_BLOCKED"


def test_validate_allowlisted_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dl, "host_suffix_allowlist", lambda: ("oaiusercontent.com",))
    out = dl.validate_download_url("https://files.oaiusercontent.com/path?tok=secret")
    assert isinstance(out, tuple)
    host, path, port = out
    assert host == "files.oaiusercontent.com"
    assert "tok=secret" in path
    assert port == 443


def test_ip_blocked_private_and_mapped() -> None:
    assert dl._ip_blocked(ipaddress.ip_address("10.0.0.1"))
    assert dl._ip_blocked(ipaddress.ip_address("127.0.0.1"))
    assert dl._ip_blocked(ipaddress.ip_address("169.254.169.254"))
    assert dl._ip_blocked(ipaddress.ip_address("::1"))
    assert dl._ip_blocked(ipaddress.ip_address("::ffff:127.0.0.1"))
    assert not dl._ip_blocked(ipaddress.ip_address("1.1.1.1"))


def test_resolve_blocks_private(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_gai(host: str, port: int, type: Any = None):  # noqa: A002
        return [
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(dl.socket, "getaddrinfo", fake_gai)
    err = dl._resolve_public_ips("files.oaiusercontent.com")
    assert isinstance(err, dict)
    assert err["error"]["code"] == "RISK_BLOCKED"
    # Error must not leak signed URL query (we only had hostname here).
    assert "?" not in str(err)


def test_download_rejects_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dl, "host_suffix_allowlist", lambda: ("oaiusercontent.com",))
    monkeypatch.setattr(dl, "_resolve_public_ips", lambda _h: ["1.2.3.4"])

    def boom(**_kwargs: Any) -> bytes:
        raise dl._DownloadError(
            "RISK_BLOCKED",
            "file download redirects are not allowed (files.oaiusercontent.com)",
            fatal=True,
        )

    monkeypatch.setattr(dl, "_https_get_pinned", boom)
    ref = dl.OpenAIFileRef(
        download_url="https://files.oaiusercontent.com/x?sig=SECRET",
        file_id="file_1",
    )
    err = dl.download_openai_file_bytes(ref)
    assert isinstance(err, dict)
    assert err["error"]["code"] == "RISK_BLOCKED"
    assert "SECRET" not in str(err)
    assert "sig=" not in str(err)


def test_sandbox_storage_host_allowed() -> None:
    """ImageGen/-mnt-data files are served from OpenAI's sandbox storage account."""
    out = dl.validate_download_url(
        "https://oaisdmntprnortheu.blob.core.windows.net/files/h2.png?sig=abc"
    )
    assert isinstance(out, tuple), out
    host, _path, port = out
    assert host == "oaisdmntprnortheu.blob.core.windows.net"
    assert port == 443


def test_other_azure_accounts_stay_blocked() -> None:
    """blob.core.windows.net is multi-tenant: only the OpenAI account prefix passes."""
    for host in (
        "evil.blob.core.windows.net",
        "blob.core.windows.net",
        # A nested label must not inherit the account's trust.
        "oaisdmntprnortheu.attacker.blob.core.windows.net",
        # Prefix must be leftmost, not anywhere in the label.
        "notoaisdmnt.blob.core.windows.net",
    ):
        err = dl.validate_download_url(f"https://{host}/x")
        assert isinstance(err, dict), host
        assert err["error"]["code"] == "RISK_BLOCKED", host


def test_account_prefix_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEAGENT_FILE_DOWNLOAD_ACCOUNT_PREFIXES", "oaifoo")
    assert dl.account_prefix_allowlist() == ("oaifoo",)
    assert dl._account_host_allowed("oaifoobar.blob.core.windows.net", ("oaifoo",))
    # Empty value disables the rule entirely (defence in depth, not a default).
    monkeypatch.setenv("CODEAGENT_FILE_DOWNLOAD_ACCOUNT_PREFIXES", "")
    assert dl.account_prefix_allowlist() == ()
    err = dl.validate_download_url("https://oaisdmntprnortheu.blob.core.windows.net/x")
    assert isinstance(err, dict)
    assert err["error"]["code"] == "RISK_BLOCKED"


def test_account_prefix_env_rejects_too_short(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEAGENT_FILE_DOWNLOAD_ACCOUNT_PREFIXES", "o,oaisdmnt")
    assert dl.account_prefix_allowlist() == ("oaisdmnt",)


def test_suffix_env_cannot_open_multi_tenant_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    """A too-broad env value must be dropped, not honoured."""
    monkeypatch.setenv(
        "CODEAGENT_FILE_DOWNLOAD_HOST_SUFFIXES",
        "blob.core.windows.net,com,acct.blob.core.windows.net,oaiusercontent.com",
    )
    assert dl.host_suffix_allowlist() == ("oaiusercontent.com",)
    err = dl.validate_download_url("https://evil.blob.core.windows.net/x")
    assert isinstance(err, dict)
    assert err["error"]["code"] == "RISK_BLOCKED"


def test_blocked_host_error_says_what_to_do_next() -> None:
    err = dl.validate_download_url("https://cdn.example.com/x?sig=SECRET")
    assert isinstance(err, dict)
    assert err["error"]["code"] == "RISK_BLOCKED"
    assert "fs_write_binary" in err["error"]["next_action"]
    assert "SECRET" not in str(err)


def test_safe_host_label_no_query() -> None:
    assert dl._safe_host_label("Files.OAIUserContent.com") == "files.oaiusercontent.com"


def test_read_http_body_stream_cap() -> None:
    class _FakeTLS:
        def __init__(self, frames: list[bytes]) -> None:
            self._frames = list(frames)

        def recv(self, _n: int) -> bytes:
            if not self._frames:
                return b""
            return self._frames.pop(0)

    oversized = b"x" * (dl.MAX_BINARY_WRITE_BYTES + 1)
    header = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(oversized)).encode() + b"\r\n\r\n"
    tls = _FakeTLS([header + oversized[:100], oversized[100:]])
    with pytest.raises(dl._DownloadError) as excinfo:
        dl._read_http_body(
            tls,  # type: ignore[arg-type]
            max_bytes=dl.MAX_BINARY_WRITE_BYTES,
            hostname="files.oaiusercontent.com",
        )
    assert excinfo.value.code == "INVALID_ARGUMENT"
    assert "exceeds" in excinfo.value.message
