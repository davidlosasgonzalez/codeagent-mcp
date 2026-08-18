"""A character cut in half by the sample boundary is not a binary file.

is_binary read the first 8192 bytes and decoded them. Any file whose byte 8192
lands inside a multi-byte character raised UnicodeDecodeError and was declared
binary — which is how a Spanish Markdown document became unreadable and
unpatchable, and needed a workaround through exec_run.
"""

from __future__ import annotations

from codeagent_mcp.fs.binary import _SAMPLE, is_binary, looks_like_utf8


def _split_a_character_at_the_boundary() -> bytes:
    """Text whose byte 8192 falls inside a two-byte character."""
    filler = b"a" * (_SAMPLE - 1)
    return filler + "ó".encode() + ("mas texto acentuado: canción, año, más. " * 50).encode()


def test_a_character_split_by_the_sample_is_still_text() -> None:
    data = _split_a_character_at_the_boundary()
    assert data[_SAMPLE - 1 : _SAMPLE + 1] == "ó".encode(), "the test must actually split one"
    assert is_binary(data) is False


def test_plain_ascii_is_text() -> None:
    assert is_binary(b"# Title\n\nhello\n") is False


def test_accented_text_shorter_than_the_sample_is_text() -> None:
    assert is_binary("evolución en mosaico — año 2026\n".encode()) is False


def test_a_nul_byte_is_binary() -> None:
    assert is_binary(b"PK\x03\x04\x00\x00garbage") is True


def test_genuinely_invalid_utf8_is_binary() -> None:
    """A lone continuation byte is not a truncation; it is wrong."""
    assert is_binary(b"text \x80\x80\x80 more") is True


def test_invalid_sequence_at_the_end_of_a_whole_file_is_binary() -> None:
    """complete=True: nothing follows, so a partial character is a broken one."""
    assert looks_like_utf8(b"abc\xc3", complete=True) is False
    assert looks_like_utf8(b"abc\xc3", complete=False) is True


def test_an_empty_file_is_text() -> None:
    assert is_binary(b"") is False
