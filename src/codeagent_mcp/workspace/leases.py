"""Exclusive workspace lease manager."""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from codeagent_mcp.errors import tool_error, tool_ok
from codeagent_mcp.workspace.lease_store import LeaseStore, LeaseStoreError
from codeagent_mcp.workspace.projects import get_project, known_projects

Mode = Literal["exclusive"]

DEFAULT_TTL_S = 2700  # 45 minutes


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_expires(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


class LeaseManager:
    """One exclusive writer lease per registered project checkout."""

    def __init__(
        self,
        store: LeaseStore,
        *,
        ttl_s: int | None = None,
        now_fn=_utcnow,
    ) -> None:
        self.store = store
        self.ttl_s = (
            ttl_s
            if ttl_s is not None
            else int(os.environ.get("CODEAGENT_LEASE_TTL_S", DEFAULT_TTL_S))
        )
        self._now = now_fn

    @classmethod
    def from_env(cls) -> LeaseManager:
        path = Path(os.environ.get("CODEAGENT_LEASE_STORE", "/var/lib/codeagent-mcp/leases.json"))
        return cls(LeaseStore(path))

    def acquire(
        self,
        *,
        project: str,
        mode: str = "exclusive",
        lease_id: str | None = None,
        holder_sub: str | None = None,
    ) -> dict[str, Any]:
        project_cfg = get_project(project)
        if project_cfg is None:
            return tool_error(
                "INVALID_ARGUMENT",
                f"unknown project {project!r}; known={list(known_projects())}",
                retryable=False,
            )
        if mode != "exclusive":
            return tool_error(
                "INVALID_ARGUMENT",
                "only mode='exclusive' is supported in V1",
                retryable=False,
            )

        try:
            return self.store.read_modify_write(
                lambda data: self._acquire_locked(
                    data,
                    project=project_cfg.name,
                    root=project_cfg.root,
                    lease_id=lease_id,
                    holder_sub=holder_sub,
                )
            )
        except LeaseStoreError as exc:
            return tool_error(
                "INTERNAL_ERROR",
                str(exc),
                retryable=True,
                next_action="Retry later; if it persists, inspect lease store on server",
            )

    def status(
        self,
        *,
        project: str | None = None,
        lease_id: str | None = None,
        holder_sub: str | None = None,
    ) -> dict[str, Any]:
        try:
            data = self.store.load()
        except LeaseStoreError as exc:
            return tool_error(
                "INTERNAL_ERROR",
                str(exc),
                retryable=True,
                next_action="Retry later; if it persists, inspect lease store on server",
            )

        now = self._now()
        leases: dict[str, Any] = data.get("leases", {})

        if lease_id:
            rec = leases.get(lease_id)
            if rec is None:
                return tool_ok(
                    status="unknown",
                    lease_id=lease_id,
                    held=False,
                    message="lease_id not found",
                )
            exp = _parse_expires(rec["expires_at"])
            if exp <= now:
                return tool_ok(
                    status="expired",
                    lease_id=lease_id,
                    project=rec["project"],
                    root=rec["root"],
                    mode=rec["mode"],
                    expires_at=rec["expires_at"],
                    held=False,
                )
            return tool_ok(
                status="held",
                lease_id=lease_id,
                project=rec["project"],
                root=rec["root"],
                mode=rec["mode"],
                expires_at=rec["expires_at"],
                held=True,
            )

        target = project or "demo"
        if get_project(target) is None:
            return tool_error(
                "INVALID_ARGUMENT",
                f"unknown project {target!r}",
                retryable=False,
            )
        active = self._active_for_project(leases, target, now)
        if active is None:
            # distinguish free vs expired leftover without leaking foreign tokens
            expired_exists = any(
                r.get("project") == target and _parse_expires(r["expires_at"]) <= now
                for r in leases.values()
            )
            return tool_ok(
                project=target,
                root=get_project(target).root,  # type: ignore[union-attr]
                status="expired" if expired_exists else "free",
                held=False,
            )
        lid, rec = active
        payload: dict[str, Any] = {
            "project": target,
            "root": rec["root"],
            "mode": rec["mode"],
            "status": "held",
            "held": True,
            "expires_at": rec["expires_at"],
        }
        # Reveal lease_id only to the authenticated holder (reconnect recovery).
        if holder_sub and rec.get("holder_sub") == holder_sub:
            payload["lease_id"] = lid
            payload["holder_match"] = True
        return tool_ok(**payload)

    def release(self, *, lease_id: str) -> dict[str, Any]:
        try:
            return self.store.read_modify_write(
                lambda data: self._release_locked(data, lease_id=lease_id)
            )
        except LeaseStoreError as exc:
            return tool_error(
                "INTERNAL_ERROR",
                str(exc),
                retryable=True,
                next_action="Retry later; if it persists, inspect lease store on server",
            )

    def require_active(self, *, lease_id: str) -> dict[str, Any]:
        """Validate lease_id, renew TTL on activity, return project/root payload.

        Missing/blank handled by callers as LEASE_REQUIRED. Unknown/expired → LEASE_EXPIRED.
        """
        if not lease_id or not str(lease_id).strip():
            return tool_error(
                "LEASE_REQUIRED",
                "lease_id is required",
                retryable=False,
                next_action="Call workspace_acquire first and pass lease_id",
            )
        lid = str(lease_id).strip()
        try:
            return self.store.read_modify_write(
                lambda data: self._require_active_locked(data, lease_id=lid)
            )
        except LeaseStoreError as exc:
            return tool_error(
                "INTERNAL_ERROR",
                str(exc),
                retryable=True,
                next_action="Retry later; if it persists, inspect lease store on server",
            )

    def _require_active_locked(
        self, data: dict[str, Any], *, lease_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        now = self._now()
        leases: dict[str, Any] = dict(data.get("leases", {}))
        leases = {
            lid: rec for lid, rec in leases.items() if _parse_expires(rec["expires_at"]) > now
        }
        rec = leases.get(lease_id)
        if rec is None:
            return (
                tool_error(
                    "LEASE_EXPIRED",
                    "lease_id is unknown or expired",
                    retryable=True,
                    next_action="Call workspace_acquire to obtain a new lease_id",
                ),
                {**data, "leases": leases},
            )
        new_exp = now + timedelta(seconds=self.ttl_s)
        rec = {**rec, "expires_at": _iso(new_exp), "renewed_at": _iso(now)}
        leases[lease_id] = rec
        return (
            tool_ok(
                lease_id=lease_id,
                project=rec["project"],
                root=rec["root"],
                mode=rec["mode"],
                expires_at=rec["expires_at"],
                status="active",
            ),
            {**data, "leases": leases},
        )

    def _acquire_locked(
        self,
        data: dict[str, Any],
        *,
        project: str,
        root: str,
        lease_id: str | None,
        holder_sub: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        now = self._now()
        leases: dict[str, Any] = dict(data.get("leases", {}))
        # drop expired records lazily (free the slot)
        leases = {
            lid: rec for lid, rec in leases.items() if _parse_expires(rec["expires_at"]) > now
        }

        active = self._active_for_project(leases, project, now)

        if lease_id:
            rec = leases.get(lease_id)
            if rec is None:
                # expired or unknown — do not steal
                if active is not None:
                    _aid, arec = active
                    return (
                        tool_error(
                            "LEASE_BUSY",
                            "workspace already held by another exclusive lease",
                            retryable=True,
                            next_action="Call workspace_status; wait for expiry or release",
                            project=project,
                            mode="exclusive",
                            current_expires_at=arec["expires_at"],
                        ),
                        {**data, "leases": leases},
                    )
                return (
                    tool_error(
                        "LEASE_EXPIRED",
                        "lease_id is unknown or expired; acquire a new lease without lease_id",
                        retryable=True,
                        next_action="Call workspace_acquire without lease_id",
                        project=project,
                    ),
                    {**data, "leases": leases},
                )
            if rec["project"] != project:
                return (
                    tool_error(
                        "INVALID_ARGUMENT",
                        "lease_id belongs to a different project",
                        retryable=False,
                    ),
                    {**data, "leases": leases},
                )
            # renew
            new_exp = now + timedelta(seconds=self.ttl_s)
            rec = {
                **rec,
                "expires_at": _iso(new_exp),
                "renewed_at": _iso(now),
            }
            if holder_sub:
                rec["holder_sub"] = holder_sub
            leases[lease_id] = rec
            return (
                tool_ok(
                    lease_id=lease_id,
                    project=project,
                    root=root,
                    mode="exclusive",
                    expires_at=rec["expires_at"],
                    status="renewed",
                ),
                {**data, "leases": leases},
            )

        if active is not None:
            aid, arec = active
            # Same authenticated holder reconnecting after losing lease_id (e.g. OAuth reauth):
            # reclaim/renew instead of LEASE_BUSY forever.
            if holder_sub and arec.get("holder_sub") and arec.get("holder_sub") == holder_sub:
                new_exp = now + timedelta(seconds=self.ttl_s)
                arec = {
                    **arec,
                    "expires_at": _iso(new_exp),
                    "renewed_at": _iso(now),
                    "holder_sub": holder_sub,
                }
                leases[aid] = arec
                return (
                    tool_ok(
                        lease_id=aid,
                        project=project,
                        root=root,
                        mode="exclusive",
                        expires_at=arec["expires_at"],
                        status="reclaimed",
                    ),
                    {**data, "leases": leases},
                )
            return (
                tool_error(
                    "LEASE_BUSY",
                    "workspace already held by an exclusive lease",
                    retryable=True,
                    next_action="Wait for expiry or renew/release with holder lease_id",
                    project=project,
                    mode="exclusive",
                    current_expires_at=arec["expires_at"],
                ),
                {**data, "leases": leases},
            )

        new_id = secrets.token_urlsafe(24)
        new_exp = now + timedelta(seconds=self.ttl_s)
        rec = {
            "project": project,
            "root": root,
            "mode": "exclusive",
            "created_at": _iso(now),
            "expires_at": _iso(new_exp),
        }
        if holder_sub:
            rec["holder_sub"] = holder_sub
        leases[new_id] = rec
        return (
            tool_ok(
                lease_id=new_id,
                project=project,
                root=root,
                mode="exclusive",
                expires_at=rec["expires_at"],
                status="acquired",
            ),
            {**data, "leases": leases},
        )

    def _release_locked(
        self, data: dict[str, Any], *, lease_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        leases: dict[str, Any] = dict(data.get("leases", {}))
        if lease_id not in leases:
            return (
                tool_ok(status="already_released", lease_id=lease_id),
                {**data, "leases": leases},
            )
        del leases[lease_id]
        return (
            tool_ok(status="released", lease_id=lease_id),
            {**data, "leases": leases},
        )

    @staticmethod
    def _active_for_project(
        leases: dict[str, Any], project: str, now: datetime
    ) -> tuple[str, dict[str, Any]] | None:
        for lid, rec in leases.items():
            if rec.get("project") != project:
                continue
            if _parse_expires(rec["expires_at"]) > now:
                return lid, rec
        return None
