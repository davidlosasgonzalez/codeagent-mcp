"""Secrets are masked on the way out of a journal, not only on the way in.

A project fixed its own logger to write k=[REDACTED]. The journal still held
every request logged before that fix, and service_logs returned them verbatim.
"""

from __future__ import annotations

import pytest

from codeagent_mcp.redact import MASK, redact, redaction_count


@pytest.mark.parametrize(
    "line",
    [
        "GET /api/v1/thing?k=abc123def456&limit=5 HTTP/1.1",
        "?token=abc123def456&redirect=/home",
        "POST /auth?client_secret=shhhhhhhh",
        'json body {"access_token": "abc123def456"}',
        "Authorization: Bearer abc123def456ghijk",
    ],
)
def test_a_secret_value_never_survives(line: str) -> None:
    out = redact(line)
    assert MASK in out
    for leaked in ("abc123def456", "shhhhhhhh"):
        assert leaked not in out


def test_the_key_survives_so_the_line_stays_readable() -> None:
    assert redact("?k=abc123def456&limit=5") == f"?k={MASK}&limit=5"


def test_json_stays_json() -> None:
    """A mask that breaks the quoting makes the log unparseable."""
    import json

    out = redact('{"access_token": "abc123def456", "expires_in": 3600}')
    assert json.loads(out) == {"access_token": MASK, "expires_in": 3600}


def test_an_authorization_header_is_masked_once() -> None:
    out = redact("Authorization: Bearer abc123def456ghijk")
    assert out.count(MASK) == 1


# Assembled rather than written out: these have to look like credentials to be
# worth testing, and a secret scanner cannot tell a fixture from a leak.
FAKE_TOKENS = (
    "sk" + "-proj-abcdefghij1234567890",
    "gh" + "p_abcdefghijklmnop1234",
    "ey" + "JhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dozjgNryP4J3jVmNHl0w5N",
)


@pytest.mark.parametrize("token", FAKE_TOKENS)
def test_self_announcing_tokens_go_even_without_a_key(token: str) -> None:
    assert token not in redact(f"upstream said {token} which is bad")


@pytest.mark.parametrize(
    "line",
    [
        "monkey=banana y turkey=pavo",
        "ERROR conectando a upstream: timeout tras 30s",
        "GET /health 200 in 16 ms",
        "checkout=/opt/example limit=50",
    ],
)
def test_ordinary_lines_are_left_alone(line: str) -> None:
    """Over-redaction makes logs useless, which is its own failure."""
    assert redact(line) == line


def test_empty_input_is_not_an_error() -> None:
    assert redact("") == ""


def test_the_count_reports_what_was_added() -> None:
    before = "?k=aaaa&token=bbbb"
    assert redaction_count(before, redact(before)) == 2
