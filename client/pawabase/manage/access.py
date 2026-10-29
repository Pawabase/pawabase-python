"""Access: API keys, secrets, custom domains and the firewall."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
        """Make a key; the key itself is in the answer once. ``scopes`` limit what it may do (``resource:read``, ``functions:invoke``…), ``allowed_ips`` and
        ``allowed_routes`` (``"GET /rest/v1/orders"``, ``"/functions/v1/*"``) limit where and what it may call."""
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
    """Secrets functions and flows read (``ctx.runtime.secret(name)``). Values are never returned."""

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


class Firewall(Service):
    """The firewall at the gateway: ordered rules (allow, block, log, rate limit), managed attack signatures, bans, and what it did.

        pb.manage.firewall.add_rule(name="Burst", action="rate_limit", conditions=[{"field": "path", "op": "starts_with", "value": "/auth/v1/token"}],
                                    rate_limit={"requests": 10, "window_seconds": 60, "by": "ip", "ban_seconds": 900})
    """

    # rules
    def rules(self) -> Any:
        """Rules in the order they are tried."""
        return self.get("/firewall")

    def add_rule(
        self, *, name: str, action: str = "block", conditions: Sequence[Mapping[str, Any]] = (), logic: str = "all", match: Mapping[str, Sequence[str]] | None = None,
        rate_limit: Mapping[str, Any] | None = None, mode: str = "enforce", enabled: bool = True, expires_at: datetime | str | None = None, note: str = "",
    ) -> Any:
        """Add a rule at the end. ``conditions`` are ``{field, op, value}`` (fields: ip, country, method, path, host, query, ``header:<name>``, ``query:<name>``,
        user_agent, origin, referer, role, key_id, content_length); ``logic`` joins them (``all``/``any``); ``mode="simulate"`` only records."""
        body: dict[str, Any] = {"name": name, "action": action, "conditions": [dict(c) for c in conditions], "logic": logic, "match": {k: list(v) for k, v in (match or {}).items()}, "mode": mode, "enabled": enabled, "note": note}
        if rate_limit:
            body["rate_limit"] = dict(rate_limit)
        if expires_at is not None:
            body["expires_at"] = expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at
        return self.post("/firewall", body)

    def update_rule(self, rule_id: str, **changes: Any) -> Any:
        """Change some fields of a rule (the same names as :meth:`add_rule`; ``clear_expiry=True`` removes an expiry)."""
        return self.patch(seg("firewall", rule_id), changes)

    def remove_rule(self, rule_id: str) -> Any:
        return self.delete(seg("firewall", rule_id))

    def order(self, rule_ids: Sequence[str]) -> Any:
        """Set the order rules are tried in: every rule exactly once."""
        return self.put("/firewall-order", {"ids": list(rule_ids)})

    # managed rules and automatic bans
    def config(self) -> Any:
        """Managed rule sets (``sqli``, ``xss``, ``traversal``, ``scanners``, ``sensitive_files``, ``no_user_agent``, ``methods``) and automatic bans."""
        return self.get("/firewall-config")

    def set_config(self, *, managed: Mapping[str, str] | None = None, auto_ban: Mapping[str, Any] | None = None) -> Any:
        """``managed``: ``{"sqli": "block", "scanners": "log"}`` (``off``, ``log``, ``block``). ``auto_ban``: ``{enabled, threshold, window_seconds, ban_seconds, statuses, exempt}``."""
        body: dict[str, Any] = {}
        if managed is not None:
            body["managed"] = dict(managed)
        if auto_ban is not None:
            body["auto_ban"] = dict(auto_ban)
        return self.put("/firewall-config", body)

    # bans
    def bans(self, *, include_expired: bool = False) -> Any:
        return self.get("/firewall-bans", all=1 if include_expired else None)

    def ban(self, ip: str, *, seconds: int = 0, reason: str = "") -> Any:
        """Ban an address or CIDR range for *seconds* (``0``: until lifted)."""
        return self.post("/firewall-bans", {"ip": ip, "seconds": seconds, "reason": reason})

    def lift_ban(self, ban_id: str) -> Any:
        return self.delete(seg("firewall-bans", ban_id))

    # what it did
    def events(self, *, action: str | None = None, source: str | None = None, ip: str | None = None, path: str | None = None, rule_id: str | None = None, limit: int = 100) -> Any:
        return self.get("/firewall-events", action=action, source=source, ip=ip, path=path, rule_id=rule_id, limit=limit)

    def stats(self, *, hours: int = 24) -> Any:
        """Totals, a timeline, and the top addresses, paths, rules and countries."""
        return self.get("/firewall-stats", hours=hours)

    def test(self, ip: str, *, path: str = "/", method: str = "GET", user_agent: str = "", query: str = "", headers: Mapping[str, str] | None = None, country: str = "", role: str = "none") -> Any:
        """What the firewall would do with a request, step by step. Nothing is sent or counted."""
        return self.post("/firewall-test", {"ip": ip, "path": path, "method": method, "user_agent": user_agent, "query": query, "headers": dict(headers or {}), "country": country, "role": role})
