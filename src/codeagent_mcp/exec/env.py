"""Child process environment for exec_run: allowlisted env overrides and umask."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from codeagent_mcp.errors import tool_error

CODEAGENT_DEFAULT_TMPDIR = Path("/var/lib/codeagent-mcp/tmp")

DEFAULT_CHILD_UMASK = 0o022

# Always rejected even if somehow listed elsewhere.
_FORBIDDEN_EXACT = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "PWD",
        "OLDPWD",
        "IFS",
        "ENV",
        "BASH_ENV",
        "CDPATH",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PERL5LIB",
        "RUBYLIB",
        "NODE_OPTIONS",
        "SSLKEYLOGFILE",
        "GIT_SSH_COMMAND",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "SSH_AUTH_SOCK",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "CODEAGENT_GITHUB_CLIENT_SECRET",
        "CODEAGENT_ALLOWED_SUBS",
        # Temp roots are pinned by the service — not client-overridable.
        "TMPDIR",
        "TEMP",
        "TMP",
    }
)

_FORBIDDEN_PREFIXES = (
    "LD_",
    "DYLD_",
    "PYTHON",
    "SSL_",
    "CURL_",
    "GIT_CONFIG",
    "CODEAGENT_",
)

_ALLOWED_EXACT = frozenset(
    {
        "TZ",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "CI",
        "NO_COLOR",
        "FORCE_COLOR",
        "TERM",
        "COLUMNS",
        "LINES",
    }
)

_ALLOWED_PREFIXES = (
    "PYTEST_",
    "TEST_",
    "UV_",
    "RUST_",
    "CARGO_",
)


def _extra_allowed_prefixes() -> tuple[str, ...]:
    """Operator-supplied prefixes, e.g. CODEAGENT_EXEC_ENV_PREFIXES=MYAPP_,DJANGO_."""
    raw = os.environ.get("CODEAGENT_EXEC_ENV_PREFIXES", "")
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def is_injectable_env_key(key: str) -> bool:
    """True when the server may set ``key`` for a child process.

    Applies to server-side configuration such as a project's ``env`` map. It is
    laxer than the client allowlist — the operator is trusted — but still refuses
    the process, loader and credential variables that must never be overridden.
    """
    if key in _FORBIDDEN_EXACT:
        return False
    return not any(key.startswith(p) for p in _FORBIDDEN_PREFIXES)


def _key_allowed(key: str) -> bool:
    """True when a *client* may override ``key`` through ``env_overrides``."""
    if not is_injectable_env_key(key):
        return False
    if key in _ALLOWED_EXACT:
        return True
    return any(key.startswith(p) for p in _ALLOWED_PREFIXES + _extra_allowed_prefixes())


def service_tmpdir(env: dict[str, str] | None = None) -> Path:
    """Return the private temp root pinned into every child process.

    Pure: it resolves the path without creating it, so callers that only need to
    *report* the directory to a client cannot fail on a read-only or unprivileged
    host. Use :func:`ensure_service_tmpdir` when the directory must exist.
    """
    source = os.environ if env is None else env
    raw = source.get("TMPDIR") or str(CODEAGENT_DEFAULT_TMPDIR)
    root = Path(raw)
    return root if root.is_absolute() else CODEAGENT_DEFAULT_TMPDIR


def ensure_service_tmpdir(env: dict[str, str] | None = None) -> Path:
    """Resolve the private temp root and create it (mode 0700) if absent."""
    root = service_tmpdir(env)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    return root


def _ensure_private_tmpdir(env: dict[str, str]) -> None:
    """Pin tempfile roots to the service private dir (not overridable via exec_run)."""
    pinned = str(ensure_service_tmpdir(env))
    env["TMPDIR"] = pinned
    env["TEMP"] = pinned
    env["TMP"] = pinned


def merge_env_overrides(overrides: dict[str, str] | None) -> dict[str, Any] | dict[str, str]:
    """Return a full env mapping or a tool_error dict (``ok: false``)."""
    env = os.environ.copy()
    _ensure_private_tmpdir(env)
    if not overrides:
        return env
    if not isinstance(overrides, dict):
        return tool_error(
            "INVALID_ARGUMENT",
            "env_overrides must be an object of string keys to string values",
            retryable=False,
        )
    for key, value in overrides.items():
        if not isinstance(key, str) or not key:
            return tool_error(
                "INVALID_ARGUMENT",
                "env_overrides keys must be non-empty strings",
                retryable=False,
            )
        if not isinstance(value, str):
            return tool_error(
                "INVALID_ARGUMENT",
                f"env_overrides[{key!r}] must be a string",
                retryable=False,
            )
        if not _key_allowed(key):
            return tool_error(
                "RISK_BLOCKED",
                f"env override {key!r} is not allowlisted",
                retryable=False,
                next_action=(
                    "Omit the key, use an allowlisted PYTEST_/TEST_/UV_/RUST_/CARGO_ "
                    "variable, or have the operator set it in the project's env map"
                ),
            )
        env[key] = value
    # Re-pin after overrides so TMPDIR cannot stick even if allowlist regresses.
    _ensure_private_tmpdir(env)
    return env


def apply_project_env(env: dict[str, str], project_env: dict[str, str]) -> dict[str, str]:
    """Overlay a project's server-side env map, then re-pin the private TMPDIR.

    Server configuration wins over client overrides: the operator wrote it, the
    client did not. Keys were validated when the registry was loaded.
    """
    if not project_env:
        return env
    out = dict(env)
    for key, value in project_env.items():
        if is_injectable_env_key(key):
            out[key] = value
    _ensure_private_tmpdir(out)
    return out


def apply_git_safe_directory(env: dict[str, str], root: str) -> dict[str, str]:
    """Let git work on a checkout the service account does not own.

    Registered roots routinely belong to another account (root, or the app's own
    user) with the service reaching them through a group. Git calls that "dubious
    ownership" and refuses the repository outright, so every git command run
    through exec_run failed on a checkout that git_status read happily — those
    tools pass the exception per invocation, and exec_run did not.

    The exception is scoped to this one child process and names the project root
    literally; it is never a wildcard and never a global git config.
    """
    out = dict(env)
    out["GIT_CONFIG_COUNT"] = "1"
    out["GIT_CONFIG_KEY_0"] = "safe.directory"
    out["GIT_CONFIG_VALUE_0"] = root
    return out


def child_umask() -> int:
    """Umask for processes spawned on a client's behalf, such as build and test runs.

    A hardened unit file sets a restrictive ``UMask=`` to protect the server's own
    state, and a child would inherit it — quietly producing files inside the project
    tree that only the server account can read, which breaks the service accounts
    that run the code. The server's private files carry an explicit mode of their
    own, so children get a conventional umask instead. Override with
    ``CODEAGENT_EXEC_UMASK`` (octal, e.g. ``027`` to keep the tree off-limits to
    everyone outside the project group).
    """
    raw = os.environ.get("CODEAGENT_EXEC_UMASK", "").strip()
    return int(raw, 8) if raw else DEFAULT_CHILD_UMASK
