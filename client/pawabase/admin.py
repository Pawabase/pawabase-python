"""``pb.admin``: administer the environment's users, organizations and roles, with the environment's secret key (``pb_sk_…``).

These are the people who sign in to *your* application, not the project's own team. Every call is scoped to the client's environment; a
user of one environment is never visible in another.

    pb.admin.users.create(email="ada@example.com", password="…", roles=["editor"])
    pb.admin.users.update(user_id, disabled=True)
    pb.admin.orgs.add_member("acme", email="ada@example.com", role="admin")
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import _core
from .manage._base import Service, seg


class _Admin(Service):
    def path(self, suffix: str = "") -> str:
        return f"/platform/v1/envs/{_core.path_of(self.env)}/auth{suffix}"


class Users(_Admin):
    def search(self, *, search: str | None = None, status: str | None = None, limit: int = 50, offset: int = 0) -> Any:
        """``search`` matches email, username and name. ``status``: ``disabled``, ``unverified``, ``locked``, ``mfa``, ``no_mfa``."""
        return self.get("/users", search=search, status=status, limit=limit, offset=offset)

    def get_user(self, user_id: str) -> Any:
        return self.get(seg("users", user_id))

    def create(
        self, *, email: str, password: str | None = None, username: str | None = None, name: str = "", email_verified: bool = True,
        user_metadata: Mapping[str, Any] | None = None, app_metadata: Mapping[str, Any] | None = None, roles: Sequence[str] = (),
    ) -> Any:
        """Create an account without the sign-up restrictions (allow-lists, password policy aside). ``app_metadata`` is for your server only."""
        body: dict[str, Any] = {"email": email, "name": name, "email_verified": email_verified, "roles": list(roles), "user_metadata": dict(user_metadata or {}), "app_metadata": dict(app_metadata or {})}
        if password is not None:
            body["password"] = password
        if username is not None:
            body["username"] = username
        return self.post("/users", body)

    def update(
        self, user_id: str, *, name: str | None = None, email_verified: bool | None = None, disabled: bool | None = None, password: str | None = None,
        user_metadata: Mapping[str, Any] | None = None, app_metadata: Mapping[str, Any] | None = None, roles: Sequence[str] | None = None,
        unlock: bool | None = None,
    ) -> Any:
        """Change what is given. ``disabled=True`` blocks sign-in and ends sessions; ``unlock=True`` clears a lockout; ``roles`` replaces the user's roles."""
        given = {"name": name, "email_verified": email_verified, "disabled": disabled, "password": password, "user_metadata": user_metadata,
                 "app_metadata": app_metadata, "roles": None if roles is None else list(roles), "unlock": unlock}
        return self.patch(seg("users", user_id), {k: v for k, v in given.items() if v is not None})

    def delete_user(self, user_id: str) -> Any:
        return self.delete(seg("users", user_id))

    def history(self, user_id: str) -> Any:
        """The user's recent sign-ins and security events."""
        return self.get(seg("users", user_id, "history"))

    def activity(self, user_id: str, *, weeks: int = 53) -> Any:
        """What the user did over the last ``weeks`` weeks (at most 53): counts per day by kind (``signin``, ``account``, ``request``),
        totals and a summary (last seen, failures, busiest hour, most used routes)."""
        return self.get(seg("users", user_id, "activity"), weeks=weeks)

    def timeline(self, user_id: str, *, type: str | None = None, limit: int = 50, before: str | None = None) -> Any:
        """The user's sign-ins, account changes and requests in one feed, newest first. ``type``: ``signin``, ``account`` or ``request``.
        Pass the answer's ``next_before`` as ``before`` for the next page."""
        return self.get(seg("users", user_id, "timeline"), type=type, limit=limit, before=before)

    def export_data(self, user_id: str) -> Any:
        """Everything the auth service holds about the user, as JSON (a data-access request). Records that the request was handled.
        Rows in your own tables are not included: they are your application's to export."""
        return self.post(seg("users", user_id, "privacy", "export"))

    def erase_data(self, user_id: str, *, confirm: str) -> Any:
        """Delete the user and clear everything that could identify them (an erasure request). Final. ``confirm`` is their email address."""
        return self.post(seg("users", user_id, "privacy", "erase"), {"confirm": confirm})

    def reset_mfa(self, user_id: str) -> Any:
        """Remove the user's second factors, so they can sign in with a password alone and enrol again."""
        return self.post(seg("users", user_id, "mfa", "reset"))

    def grant(self, user_id: str, permissions: Sequence[str]) -> Any:
        """Give the user permissions directly, beyond their roles' (``posts:write``, ``billing:*``)."""
        return self.post(seg("users", user_id, "permissions"), {"permissions": list(permissions)})

    def revoke(self, user_id: str, permissions: Sequence[str]) -> Any:
        return self._c._call("DELETE", self.path(seg("users", user_id, "permissions")), json={"permissions": list(permissions)})

    def revoke_sessions(self, user_id: str) -> Any:
        """Sign the user out everywhere."""
        return self.post(seg("users", user_id, "sessions", "revoke"))

    def revoke_session(self, user_id: str, session_id: str) -> Any:
        return self.delete(seg("users", user_id, "sessions", session_id))

    def devices(self, user_id: str) -> Any:
        return self.get(seg("users", user_id, "devices"))

    def forget_device(self, user_id: str, device_id: str) -> Any:
        return self.delete(seg("users", user_id, "devices", device_id))

    def api_keys(self, user_id: str) -> Any:
        """The user's own API keys (never the secrets)."""
        return self.get(seg("users", user_id, "api-keys"))


class Privacy(_Admin):
    def requests(self, *, limit: int = 50) -> Any:
        """The data requests handled so far (exports and erasures). They hold no personal data: the person's id and a hash of their address."""
        return self.get("/privacy/requests", limit=limit)


class Roles(_Admin):
    def list(self) -> Any:
        """Roles with their permissions."""
        return self.get("/roles")

    def put(self, name: str, permissions: Sequence[str], *, description: str = "") -> Any:  # type: ignore[override]
        """Create or replace a role. Names are lowercase (``editor``, ``org:billing``); permissions are ``resource:action`` patterns with ``*`` wildcards."""
        return Service.put(self, "/roles", {"name": name, "description": description, "permissions": list(permissions)})

    def remove(self, name: str) -> Any:
        return self.delete(seg("roles", name))


class Orgs(_Admin):
    """Organizations (tenants) and their members. A member's role is ``owner``, ``admin`` or ``member``."""

    def list(self) -> Any:
        return self.get("/orgs")

    def get_org(self, slug: str) -> Any:
        return self.get(seg("orgs", slug))

    def create(self, slug: str, name: str, *, owner_email: str | None = None, metadata: Mapping[str, Any] | None = None) -> Any:
        body: dict[str, Any] = {"slug": slug, "name": name, "metadata": dict(metadata or {})}
        if owner_email:
            body["owner_email"] = owner_email
        return self.post("/orgs", body)

    def update(self, slug: str, *, name: str | None = None, metadata: Mapping[str, Any] | None = None) -> Any:
        return self.patch(seg("orgs", slug), {k: v for k, v in {"name": name, "metadata": metadata}.items() if v is not None})

    def remove(self, slug: str) -> Any:
        return self.delete(seg("orgs", slug))

    def add_member(self, slug: str, *, email: str, role: str = "member") -> Any:
        """Add an existing user straight in."""
        return self.post(seg("orgs", slug, "members"), {"email": email, "role": role})

    def set_member_role(self, slug: str, user_id: str, role: str) -> Any:
        return self.patch(seg("orgs", slug, "members", user_id), {"role": role})

    def remove_member(self, slug: str, user_id: str) -> Any:
        return self.delete(seg("orgs", slug, "members", user_id))

    def invite(self, slug: str, *, email: str, role: str = "member", redirect_to: str | None = None) -> Any:
        """Mail an invitation; ``redirect_to`` must be one of the environment's allowed redirect URLs."""
        return self.post(seg("orgs", slug, "invitations"), {"email": email, "role": role, **({"redirect_to": redirect_to} if redirect_to else {})})


class Invitations(_Admin):
    def list(self) -> Any:
        """Pending and past invitations across organizations."""
        return self.get("/invitations")

    def resend(self, invitation_id: str, *, redirect_to: str | None = None) -> Any:
        return self.post(seg("invitations", invitation_id, "resend"), {"redirect_to": redirect_to} if redirect_to else {})

    def revoke(self, invitation_id: str) -> Any:
        return self.delete(seg("invitations", invitation_id))


class Sessions(_Admin):
    def list(self, *, limit: int | None = None) -> Any:
        """Active sessions across users."""
        return self.get("/sessions", limit=limit)

    def revoke(self, family: str) -> Any:
        """End one session (all its refresh tokens)."""
        return self.delete(seg("sessions", family))


class ApiKeys(_Admin):
    """API keys users made for themselves."""

    def list(self, *, limit: int | None = None) -> Any:
        return self.get("/api-keys", limit=limit)

    def revoke(self, key_id: str) -> Any:
        return self.delete(seg("api-keys", key_id))


class Devices(_Admin):
    def list(self, *, limit: int | None = None) -> Any:
        """Recent devices people signed in from."""
        return self.get("/devices", limit=limit)


class Insight(_Admin):
    """Events, statistics, backup and the hosted pages."""

    def events(self, *, kind: str | None = None, failed: bool = False, limit: int = 100) -> Any:
        """Authentication events (sign-ins, sign-ups, resets, MFA…). ``failed=True`` for failures only."""
        return self.get("/events", kind=kind, failed="true" if failed else None, limit=limit)

    def stats(self) -> Any:
        """Counts of users, sign-ins, MFA use and failures."""
        return self.get("/stats")

    def backup(self) -> Any:
        """Every identity (users, organizations, roles, factors) as a document."""
        return self.get("/backup")

    def restore(self, identities: Mapping[str, Any], *, replace: bool = False, dry_run: bool = False) -> Any:
        """Apply the ``identities`` of a backup. ``dry_run=True`` reports what would change."""
        return self.post("/restore", {"identities": dict(identities), "replace": replace, "dry_run": dry_run})

    def preview_token_profile(self, profile: Mapping[str, Any], user_id: str, *, org: str | None = None) -> Any:
        """The claims a token profile would put in a token for this user (and organization), without issuing one."""
        body = {"profile": dict(profile), "user_id": user_id, **({"org": org} if org else {})}
        return self.post("/token-profiles/preview", body)

    def hosted_source(self, page: str, *, branding: Mapping[str, Any] | None = None, form: Mapping[str, Any] | None = None) -> Any:
        """The HTML a hosted sign-in page is served as for the branding being edited (the saved branding when omitted)."""
        return self.post("/hosted-source", {"page": page, **({"branding": dict(branding)} if branding is not None else {}), **({"form": dict(form)} if form is not None else {})})


class Admin:
    users: Users
    roles: Roles
    orgs: Orgs
    invitations: Invitations
    sessions: Sessions
    api_keys: ApiKeys
    devices: Devices
    insight: Insight
    privacy: Privacy

    def __init__(self, client: Any, environment: str | None = None) -> None:
        self._client = client
        self._environment = environment
        self.users = Users(client, environment)
        self.roles = Roles(client, environment)
        self.orgs = Orgs(client, environment)
        self.invitations = Invitations(client, environment)
        self.sessions = Sessions(client, environment)
        self.api_keys = ApiKeys(client, environment)
        self.devices = Devices(client, environment)
        self.insight = Insight(client, environment)
        self.privacy = Privacy(client, environment)

    def environment(self, name: str) -> Admin:
        return Admin(self._client, name)
