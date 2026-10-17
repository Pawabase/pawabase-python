"""Lifecycle: environments, previews, promotion, blueprints, branches and releases, backups and snapshots."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from ._base import Service, seg


class Environments(Service):
    """The project's environments (``development``, ``production``, previews…). These calls are not scoped to the client's environment."""

    def _project(self, suffix: str = "") -> str:
        return "/platform/v1/envs" + suffix

    def list(self) -> Any:
        return self._c._call("GET", self._project(), retry=True)

    def create(self, name: str, *, copy_from: str | None = None) -> Any:
        """A new environment, optionally with another's definitions copied in."""
        return self._c._call("POST", self._project(), json={"name": name, **({"copy_from": copy_from} if copy_from else {})})

    def get_one(self, name: str | None = None) -> Any:
        return self._c._call("GET", self._project(seg(name or self.env)), retry=True)

    def update(self, name: str, *, infra: Mapping[str, Any] | None = None, auth: Mapping[str, Any] | None = None, settings: Mapping[str, Any] | None = None, is_default: bool | None = None) -> Any:
        """Change an environment's infrastructure, sign-in settings or settings; each is merged in."""
        body = {k: v for k, v in {"infra": infra, "auth": auth, "settings": settings, "is_default": is_default}.items() if v is not None}
        return self._c._call("PATCH", self._project(seg(name)), json=body)

    def delete_environment(self, name: str) -> Any:
        return self._c._call("DELETE", self._project(seg(name)))

    def create_preview(
        self, ref: str, *, name: str | None = None, expires_in_hours: int = 72, infra: Mapping[str, Any] | None = None,
        auth: Mapping[str, Any] | None = None, settings: Mapping[str, Any] | None = None,
    ) -> Any:
        """A short-lived copy for a branch or pull request (``ref``: lowercase letters, digits and hyphens); it expires by itself."""
        body: dict[str, Any] = {"ref": ref, "expires_in_hours": expires_in_hours, "infra": dict(infra or {}), "auth": dict(auth or {})}
        if name:
            body["name"] = name
        if settings is not None:
            body["settings"] = dict(settings)
        return self.post("/previews", body)

    def delete_preview(self, preview: str) -> Any:
        return self.delete(seg("previews", preview))

    def promote(self, to: str, *, include: Sequence[str] | None = None) -> Any:
        """Copy this environment's definitions (never secrets or keys) to *to*. ``include`` limits the kinds (``flows``, ``policies``, ``resources``…)."""
        return self.post("/promote", {"to": to, **({"include": list(include)} if include is not None else {})})

    def blueprint(self) -> Any:
        """Export definitions, roles and settings as a document another project can apply."""
        return self.get("/blueprint")

    def apply_blueprint(self, blueprint: Mapping[str, Any], environments: Sequence[str] = ("development", "production")) -> Any:
        """Build new environments from a blueprint; each must not exist yet. Not scoped to an environment."""
        return self._c._call("POST", "/platform/v1/blueprints/apply", json={"blueprint": dict(blueprint), "environments": list(environments)})

    def reload_code(self) -> Any:
        """Re-read the deployed function code now."""
        return self._c._call("POST", "/platform/v1/code/reload")


class Releases(Service):
    """Feature branches of the definitions, immutable revisions, API versions and releases."""

    # branches
    def branches(self) -> Any:
        return self.get("/branches")

    def create_branch(self, name: str, *, from_branch: str | None = None, from_revision: str | None = None, protected: bool = False) -> Any:
        return self.post("/branches", {k: v for k, v in {"name": name, "from_branch": from_branch, "from_revision": from_revision, "protected": protected}.items() if v is not None})

    def draft(self, branch: str) -> Any:
        """The branch's definitions as they stand now."""
        return self.get(seg("branches", branch, "draft"))

    def merge(self, branch: str, *, target: str = "main", force: bool = False) -> Any:
        return self.post(seg("branches", branch, "merge"), {"target": target, "force": force})

    # revisions
    def revisions(self) -> Any:
        return self.get("/revisions")

    def revision(self, revision_id: str) -> Any:
        return self.get(seg("revisions", revision_id))

    def commit(self, branch: str, message: str = "") -> Any:
        """Freeze the branch's draft as a revision."""
        return self.post(seg("branches", branch, "revisions"), {"message": message})

    # api versions
    def api_versions(self) -> Any:
        return self.get("/api-versions")

    def create_api_version(self, name: str, *, is_default: bool = False) -> Any:
        return self.post("/api-versions", {"name": name, "is_default": is_default})

    def update_api_version(self, name: str, *, status: str | None = None, is_default: bool | None = None, sunset_at: datetime | str | None = None) -> Any:
        """``status``: draft, active, deprecated, sunset, disabled."""
        when = sunset_at.isoformat() if isinstance(sunset_at, datetime) else sunset_at
        return self.patch(seg("api-versions", name), {k: v for k, v in {"status": status, "is_default": is_default, "sunset_at": when}.items() if v is not None})

    def rollback_api_version(self, name: str) -> Any:
        return self.post(seg("api-versions", name, "rollback"))

    # releases
    def releases(self) -> Any:
        return self.get("/releases")

    def release(self, revision_id: str, api_version: str, name: str, *, notes: str = "", allow_breaking: bool = False) -> Any:
        """Make a revision the live definitions of an API version."""
        return self.post("/releases", {"revision_id": revision_id, "api_version": api_version, "name": name, "notes": notes, "allow_breaking": allow_breaking})

    def activate(self, release_id: str) -> Any:
        return self.post(seg("releases", release_id, "activate"))

    def deployments(self) -> Any:
        return self.get("/deployments")


class Backups(Service):
    """Whole-environment backups you download, stored snapshots, and restores."""

    def download(self, *, include: Sequence[str] | None = None, data: bool = False, max_rows: int | None = None) -> Any:
        """A backup document: ``definitions``, ``settings``, ``users`` and, with ``data=True``, rows (up to ``max_rows`` per table)."""
        return self.get("/backup", include=",".join(include) if include else None, data="true" if data else None, max_rows=max_rows)

    def restore(
        self, backup: Mapping[str, Any], *, include: Sequence[str] | None = None, strategy: str = "merge", dry_run: bool = False, confirm: str | None = None,
    ) -> Any:
        """Put a downloaded backup back. ``strategy="replace"`` first removes what the chosen parts hold and needs ``confirm`` = the environment's name."""
        body: dict[str, Any] = {"backup": dict(backup), "strategy": strategy, "dry_run": dry_run}
        if include is not None:
            body["include"] = list(include)
        if confirm:
            body["confirm"] = confirm
        return self.post("/restore", body)

    def set_schedule(
        self, *, enabled: bool = True, frequency: str = "daily", hour: int = 3, weekday: int = 0, keep_last: int = 7, keep_days: int = 30, include: Sequence[str] = (),
    ) -> Any:
        """Automatic snapshots: ``frequency`` hourly, daily (at ``hour`` UTC) or weekly (on ``weekday``, 0 = Monday); older ones are pruned past ``keep_last`` and ``keep_days``."""
        return self.put("/backup-schedule", {"enabled": enabled, "frequency": frequency, "hour": hour, "weekday": weekday, "keep_last": keep_last, "keep_days": keep_days, "include": list(include)})

    # snapshots
    def snapshots(self) -> Any:
        return self.get("/snapshots")

    def snapshot(self, name: str = "", *, note: str = "", include: Sequence[str] | None = None) -> Any:
        """Take a snapshot now and keep it."""
        return self.post("/snapshots", {"name": name, "note": note, **({"include": list(include)} if include is not None else {})})

    def update_snapshot(self, snapshot_id: str, *, name: str | None = None, note: str | None = None, pinned: bool | None = None) -> Any:
        """Rename it, annotate it, or pin it so pruning keeps it."""
        return self.patch(seg("snapshots", snapshot_id), {k: v for k, v in {"name": name, "note": note, "pinned": pinned}.items() if v is not None})

    def remove_snapshot(self, snapshot_id: str) -> Any:
        return self.delete(seg("snapshots", snapshot_id))

    def download_snapshot(self, snapshot_id: str) -> Any:
        return self.get(seg("snapshots", snapshot_id, "download"))

    def verify_snapshot(self, snapshot_id: str) -> Any:
        """Check the stored snapshot is intact."""
        return self.post(seg("snapshots", snapshot_id, "verify"))

    def restore_snapshot(
        self, snapshot_id: str, *, include: Sequence[str] | None = None, strategy: str = "merge", dry_run: bool = False, confirm: str | None = None,
        safety_snapshot: bool = True,
    ) -> Any:
        """Restore a snapshot; a snapshot of the current state is taken first unless ``safety_snapshot=False``."""
        body: dict[str, Any] = {"strategy": strategy, "dry_run": dry_run, "safety_snapshot": safety_snapshot}
        if include is not None:
            body["include"] = list(include)
        if confirm:
            body["confirm"] = confirm
        return self.post(seg("snapshots", snapshot_id, "restore"), body)
