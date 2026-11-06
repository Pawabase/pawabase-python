"""Access: API keys, secrets, custom domains and the firewall."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from ._base import Service, seg


class Keys(Service):
    """The environment's API keys: publishable (``pb_pk_…``, safe in apps) and secret (``pb_sk_…``, servers only)."""

    def list(self) -> Any:
        return self.get("/keys")

    def create(
        self, name: str, *, role: str = "publishable", scopes: Sequence[str] = (), expires_at: datetime | str | None = None,
        allowed_ips: Sequence[str] = (), allowed_routes: Sequence[str] = (),
    ) -> Any:
        """Make a key; the key itself is in the answer once. ``scopes`` limit what it may do (``resource:read``, ``routes:invoke``…), ``allowed_ips`` and
        ``allowed_routes`` (``"GET /rest/v1/orders"``, ``"/flows/v1/*"``) limit where and what it may call."""
        body: dict[str, Any] = {"name": name, "role": role, "scopes": list(scopes), "allowed_ips": list(allowed_ips), "allowed_routes": list(allowed_routes)}
        if expires_at is not None:
            body["expires_at"] = expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at
        return self.post("/keys", body)

    def scopes(self) -> Any:
        """Every scope a key can hold, with what each allows (the choices for ``create(scopes=...)``)."""
        return self._c._call("GET", "/platform/v1/key-scopes", retry=True)

    def revoke(self, key_id: str) -> Any:
        return self.post(seg("keys", key_id, "revoke"))

    def rotate(self, key_id: str, *, grace_hours: int = 24) -> Any:
        """Replace a key with a new one; the old keeps working for *grace_hours* so clients can switch."""
        return self.post(seg("keys", key_id, "rotate"), {"grace_hours": grace_hours})


class Secrets(Service):
    """Secrets flows read (``ctx.runtime.secret(name)``). Values are never returned."""

    def list(self) -> Any:
        return self.get("/secrets")

    def set(self, name: str, value: str, *, description: str = "", rotate_every_days: int | None = None) -> Any:
        body: dict[str, Any] = {"value": value, "description": description}
        if rotate_every_days is not None:
            body["rotate_every_days"] = rotate_every_days
        return self.put(seg("secrets", name), body)

    def update(self, name: str, *, description: str | None = None, rotate_every_days: int | None = None) -> Any:
        """Change the description or the rotation reminder (``0`` clears it)."""
        return self.patch(seg("secrets", name), {k: v for k, v in {"description": description, "rotate_every_days": rotate_every_days}.items() if v is not None})

    def rotate(self, name: str, value: str | None = None) -> Any:
        """Replace the value, keeping the old one for :meth:`rollback`."""
        return self.post(seg("secrets", name, "rotate"), {"value": value} if value else {})

    def rollback(self, name: str) -> Any:
        """Put back the value a rotation replaced."""
        return self.post(seg("secrets", name, "rollback"))

    def remove(self, name: str) -> Any:
        return self.delete(seg("secrets", name))


class Domains(Service):
    """Custom domains the environment answers on, proved with a DNS record."""

    def list(self) -> Any:
        return self.get("/domains")

    def add(self, hostname: str) -> Any:
        """Register a hostname: the answer says which DNS record proves you own it."""
        return self.post("/domains", {"hostname": hostname})

    def verify(self, domain_id: str) -> Any:
        """Check the DNS record now."""
        return self.post(seg("domains", domain_id, "verify"))

    def remove(self, domain_id: str) -> Any:
        return self.delete(seg("domains", domain_id))
