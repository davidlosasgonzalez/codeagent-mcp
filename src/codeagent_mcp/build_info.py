"""Which build is actually running.

A semver alone cannot answer "which build produced this behaviour" on a host
that is redeployed from a working tree: the version string is identical before
and after, and the tree may carry changes no commit describes. The stamp
records the commit, whether the tree was dirty, and when it was installed.

Two deliberate choices:

* The stamp is read **once, at import**. Re-reading per call would report a
  stamp a later deploy wrote but this process never loaded — the exact lie the
  stamp exists to prevent.
* There is no fallback to running ``git``. The service account has no business
  reading the repo, and a guessed commit is worse than an absent one. With no
  stamp the fields are ``None`` and ``source`` says ``unstamped``.

The file lives outside the checkout and is root-owned, so the service cannot
write its own identity.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final

DEFAULT_STAMP_PATH: Final = "/etc/codeagent-mcp/build.json"
STAMP_PATH_ENV: Final = "CODEAGENT_BUILD_STAMP"

# Only these are ever republished. A stamp file with extra keys does not get to
# decide what server_info exposes.
_FIELDS: Final = ("commit", "dirty", "deployed_at", "note")

_UNSTAMPED: Final[dict[str, Any]] = {
    "commit": None,
    "dirty": None,
    "deployed_at": None,
    "note": None,
    "source": "unstamped",
}


def stamp_path() -> Path:
    """Where the build stamp is expected to be."""
    return Path(os.environ.get(STAMP_PATH_ENV) or DEFAULT_STAMP_PATH)


def read_build_stamp(path: Path | None = None) -> dict[str, Any]:
    """Read the stamp, or report that there is none.

    Every failure mode — missing, unreadable, malformed, not an object — lands
    on the same honest answer instead of raising: ``server_info`` must keep
    working on a host that was never stamped.
    """
    target = path if path is not None else stamp_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_UNSTAMPED)
    if not isinstance(raw, dict):
        return dict(_UNSTAMPED)

    out: dict[str, Any] = {key: raw.get(key) for key in _FIELDS}
    out["dirty"] = bool(out["dirty"]) if out["dirty"] is not None else None
    out["source"] = "stamp"
    return out


# Read at import: this is the build this process is running.
BUILD_STAMP: Final[dict[str, Any]] = read_build_stamp()


def build_stamp() -> dict[str, Any]:
    """The stamp loaded when this process started."""
    return dict(BUILD_STAMP)
