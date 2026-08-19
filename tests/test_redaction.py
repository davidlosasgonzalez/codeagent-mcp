"""Secrets are masked on the way out of a journal, not only on the way in.

A project fixed its own logger to write k=[REDACTED]. The journal still held
every request logged before that fix, and service_logs returned them verbatim.

Every credential-shaped value here is **built at run time**. Splitting a literal
in half was not enough — the remaining half still reads as a JWT to a scanner,
and two incidents were raised against this file. Synthesising them from parts
that are individually meaningless leaves the source with nothing to match, and
the tests still see exactly the shapes they are meant to catch.
"""

from __future__ import annotations

import base64
import json

import pytest

from codeagent_mcp.redact import MASK, redact, redaction_count

# A value with no meaning that is long enough to look like one.
VALUE = "".join(chr(ord("a") + i % 26) for i in range(12))
LONGER = VALUE + "ghijk"


def _b64(payload: dict[str, str]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def _jwt() -> str:
    """A structurally valid JWT: three base64url segments, no secret anywhere."""
    return f"{_b64({'alg': 'HS256'})}.{_b64({'sub': '1234'})}.{'a' * 22}"


def _prefixed(prefix: str, length: int) -> str:
    """A token that announces itself by prefix, e.g. an API key shape."""
    return prefix + "".join(chr(ord("a") + i % 26) for i in range(length))


@pytest.mark.parametrize(
    "template",
    [
        "GET /api/v1/thing?k={v}&limit=5 HTTP/1.1",
        "?token={v}&redirect=/home",
        "POST /auth?client_secret={v}",
        'json body {{"access_token": "{v}"}}',
        "Authorization: Bearer {v}",
    ],
)
def test_a_secret_value_never_survives(template: str) -> None:
    out = redact(template.format(v=LONGER))
    assert MASK in out
    assert LONGER not in out


def test_the_key_survives_so_the_line_stays_readable() -> None:
    assert redact(f"?k={VALUE}&limit=5") == f"?k={MASK}&limit=5"


def test_json_stays_json() -> None:
    """A mask that breaks the quoting makes the log unparseable."""
    out = redact(json.dumps({"access_token": VALUE, "expires_in": 3600}))
    assert json.loads(out) == {"access_token": MASK, "expires_in": 3600}


def test_an_authorization_header_is_masked_once() -> None:
    out = redact(f"Authorization: Bearer {LONGER}")
    assert out.count(MASK) == 1


def _self_announcing() -> tuple[str, ...]:
    return (_prefixed("sk-", 24), _prefixed("ghp_", 20), _jwt())


@pytest.mark.parametrize("index", range(3))
def test_self_announcing_tokens_go_even_without_a_key(index: int) -> None:
    token = _self_announcing()[index]
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
    before = f"?k={VALUE}&token={LONGER}"
    assert redaction_count(before, redact(before)) == 2


def test_the_fixtures_really_do_look_like_credentials() -> None:
    """Otherwise this file would pass by testing nothing.

    Building the values at run time is only safe if they still have the shape
    the patterns look for; this is what stops the synthesis from quietly
    defanging the suite.
    """
    jwt = _jwt()
    assert jwt.count(".") == 2
    assert jwt.startswith("eyJ"), "a JWT starts with a base64url JSON header"
    assert len(LONGER) >= 8, "shorter than the bearer pattern requires"
    assert _prefixed("sk-", 24).startswith("sk-")
