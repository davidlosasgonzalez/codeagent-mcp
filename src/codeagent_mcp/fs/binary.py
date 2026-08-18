"""Binary-vs-text detection for fs_read."""

from __future__ import annotations

import codecs

_SAMPLE = 8192


def looks_like_utf8(sample: bytes, *, complete: bool) -> bool:
    """Decode ``sample`` as UTF-8, tolerating a character cut in half.

    ``complete=False`` means the caller handed us a prefix of a larger file, so
    a trailing partial sequence is expected rather than wrong. An incremental
    decoder holds those bytes back instead of raising; a genuinely invalid
    sequence still raises, which is what we are testing for.

    Without this, a file was called binary because byte 8192 landed in the
    middle of an accented character.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        decoder.decode(sample, final=complete)
    except UnicodeDecodeError:
        return False
    return True


def is_binary(data: bytes) -> bool:
    """Return True if the leading sample looks binary."""
    sample = data[:_SAMPLE]
    if b"\x00" in sample:
        return True
    return not looks_like_utf8(sample, complete=len(data) <= _SAMPLE)
