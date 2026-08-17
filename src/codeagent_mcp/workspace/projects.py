"""Server-side project registry. Clients never supply filesystem roots.

Projects are loaded from a YAML file (CODEAGENT_PROJECTS_FILE). The public
repository ships deploy/projects.example.yaml with a demo root only; operators
install a host-local projects.yaml with their own checkouts.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

# Optional aliases can provide stable environment-specific project identifiers.
# Do not rename identifiers to bypass client-side safety controls.


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """One registered project: stable id, absolute root, and write enablement.

    ``control_socket`` and ``health_url`` are optional and enable the service
    control tools for this project. See ``docs/product/service-control.md``.
    """

    name: str
    root: str
    writable: bool = False
    env: dict[str, str] = field(default_factory=dict)
    control_socket: str | None = None
    health_url: str | None = None
    runtime_paths: dict[str, str] = field(default_factory=dict)


def _parse_env(raw: Any, project: str) -> dict[str, str]:
    """Validate the optional per-project env map from the registry file."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"project {project!r}: 'env' must be a mapping")
    from codeagent_mcp.exec.env import is_injectable_env_key

    out: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key)
        if not is_injectable_env_key(name):
            raise ValueError(
                f"project {project!r}: env key {name!r} is not allowed "
                "(process, loader and credential variables are reserved)"
            )
        out[name] = str(value)
    return out


def _parse_control_socket(raw: Any, project: str) -> str | None:
    """Validate the optional privileged control socket path."""
    if raw is None:
        return None
    path = str(raw).strip()
    if not path.startswith("/"):
        raise ValueError(f"project {project!r}: 'control_socket' must be an absolute path")
    return path


# The health probe runs inside the server and its response body is handed back to
# the client, so anything but loopback would turn the registry into a request
# forwarder for whatever the host can reach.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _parse_health_url(raw: Any, project: str) -> str | None:
    """Validate the optional health probe URL: http(s) on loopback only."""
    if raw is None:
        return None
    url = str(raw).strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"project {project!r}: 'health_url' must be an http or https URL")
    if (parsed.hostname or "") not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"project {project!r}: 'health_url' must point at loopback "
            f"({', '.join(sorted(_LOOPBACK_HOSTS))}), not {parsed.hostname!r}"
        )
    return url


_RUNTIME_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# Anything a client could read here is outside the project checkout, so the only
# safe grant is one an operator typed on purpose: named, absolute, read-only.
_RUNTIME_FORBIDDEN_ROOTS = ("/etc", "/root", "/proc", "/sys", "/dev", "/boot")


def _parse_runtime_paths(raw: Any, project: str) -> dict[str, str]:
    """Validate the optional read-only runtime views (name -> absolute directory).

    These are *not* part of the writable project jail: they let a caller inspect
    the data a deployed service actually runs on (a database directory, a state
    dir) without handing it the filesystem. Reading still needs POSIX permission
    — declaring a path authorizes it, it does not grant access to it.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"project {project!r}: 'runtime_paths' must be a mapping of name to path")
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key)
        if not _RUNTIME_NAME_RE.match(name):
            raise ValueError(
                f"project {project!r}: runtime path name {name!r} must match "
                f"{_RUNTIME_NAME_RE.pattern}"
            )
        path = str(value).strip()
        if not path.startswith("/"):
            raise ValueError(f"project {project!r}: runtime path {name!r} must be absolute")
        normalized = os.path.normpath(path)
        if normalized == "/" or any(
            normalized == bad or normalized.startswith(bad + "/")
            for bad in _RUNTIME_FORBIDDEN_ROOTS
        ):
            raise ValueError(
                f"project {project!r}: runtime path {name!r} points at a reserved "
                f"system location ({normalized})"
            )
        out[name] = normalized
    return out


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "0").strip() == "1"


def _default_projects_path() -> Path:
    """Resolve the projects file: env override, then /etc, then example in repo."""
    override = os.environ.get("CODEAGENT_PROJECTS_FILE", "").strip()
    if override:
        return Path(override)
    etc = Path("/etc/codeagent-mcp/projects.yaml")
    if etc.is_file():
        return etc
    repo_example = Path(__file__).resolve().parents[3] / "deploy" / "projects.example.yaml"
    if repo_example.is_file():
        return repo_example
    return etc


def _parse_entry(raw: dict[str, Any]) -> ProjectConfig:
    name = str(raw.get("id") or raw.get("name") or "").strip()
    root = str(raw.get("root") or "").strip()
    if not name or not root:
        raise ValueError("each project requires id (or name) and root")
    if "writable_env" in raw and raw["writable_env"]:
        writable = _env_flag(str(raw["writable_env"]).strip())
    else:
        writable = bool(raw.get("writable", False))
    return ProjectConfig(
        name=name,
        root=root,
        writable=writable,
        env=_parse_env(raw.get("env"), name),
        control_socket=_parse_control_socket(raw.get("control_socket"), name),
        health_url=_parse_health_url(raw.get("health_url"), name),
        runtime_paths=_parse_runtime_paths(raw.get("runtime_paths"), name),
    )


def _load_registry_from_path(path: Path) -> dict[str, ProjectConfig]:
    if not path.is_file():
        raise FileNotFoundError(
            f"projects file not found: {path}; "
            "set CODEAGENT_PROJECTS_FILE or install /etc/codeagent-mcp/projects.yaml"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "projects" not in data:
        raise ValueError(f"projects file {path} must contain a top-level 'projects' list")
    entries = data["projects"]
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"projects file {path} has empty 'projects' list")
    registry: dict[str, ProjectConfig] = {}
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError(f"invalid project entry in {path}: {item!r}")
        cfg = _parse_entry(item)
        registry[cfg.name] = cfg
    return registry


def _registry() -> dict[str, ProjectConfig]:
    """Return the current project map (re-reads file so writable_env tracks env)."""
    return _load_registry_from_path(_default_projects_path())


def get_project(name: str) -> ProjectConfig | None:
    """Return the registered project for ``name``, or ``None`` if unknown."""
    return _registry().get(name)


def known_projects() -> tuple[str, ...]:
    """Return sorted project ids from the current registry file."""
    return tuple(sorted(_registry()))
