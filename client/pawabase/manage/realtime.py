"""Realtime channel rules and dashboards: who may subscribe and publish where, and the read-only boards built over your data."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._base import Service, seg

CHANNEL_KINDS = ("public", "private", "presence", "protected", "ephemeral", "durable")


class RealtimeChannels(Service):
    """Durable channel rules: a channel name pattern (``chat:*``), its kind, who may subscribe and publish (a policy name), presence, history and limits."""

    def list(self) -> Any:
        return self.get("/realtime-channels")

    def create(
        self, pattern: str, *, kind: str = "public", subscribe: str = "authenticated", publish: str = "authenticated", presence: bool = True,
        history: int = 50, message_size_limit: int = 0, publish_rate_limit: int = 0, subscription_limit: int = 0, retention_seconds: int = 0,
        description: str = "",
    ) -> Any:
        """Add a rule for channels matching *pattern*. A limit of ``0`` means none; ``history`` is how many recent messages are kept (0 to 1000)."""
        return self.post(
            "/realtime-channels",
            {
                "pattern": pattern, "kind": kind, "subscribe": subscribe, "publish": publish, "presence": presence, "history": history,
                "message_size_limit": message_size_limit, "publish_rate_limit": publish_rate_limit, "subscription_limit": subscription_limit,
                "retention_seconds": retention_seconds, "description": description,
            },
        )

    def update(self, channel_id: str, **changes: Any) -> Any:
        """Change a rule: any of ``kind``, ``subscribe``, ``publish``, ``presence``, ``history``, ``message_size_limit``, ``publish_rate_limit``,
        ``subscription_limit``, ``retention_seconds``, ``description``."""
        return self.patch(seg("realtime-channels", channel_id), changes)

    def remove(self, channel_id: str) -> Any:
        return self.delete(seg("realtime-channels", channel_id))


class Dashboards(Service):
    """Dashboards: read-only boards over your data, defined as JSON, optionally published at a public address."""

    def list(self) -> Any:
        return self.get("/dashboards")

    def get_one(self, dashboard_id: str) -> Any:
        """One dashboard with its full definition."""
        return self.get(seg("dashboards", dashboard_id))

    def create(self, name: str, definition: Mapping[str, Any] | None = None, *, title: str = "", description: str = "", enabled: bool = True) -> Any:
        """Make a dashboard (``name``: lowercase letters, digits and dashes). The definition is checked; a problem comes back as a 422 with the list."""
        return self.post("/dashboards", {"name": name, "title": title, "description": description, "definition": dict(definition or {"pages": []}), "enabled": enabled})

    def update(self, dashboard_id: str, *, title: str | None = None, description: str | None = None, definition: Mapping[str, Any] | None = None, enabled: bool | None = None) -> Any:
        body = {"title": title, "description": description, "definition": dict(definition) if definition is not None else None, "enabled": enabled}
        return self.patch(seg("dashboards", dashboard_id), {k: v for k, v in body.items() if v is not None})

    def remove(self, dashboard_id: str) -> Any:
        return self.delete(seg("dashboards", dashboard_id))

    def publish(self, dashboard_id: str, *, public: bool, publishable_key: str | None = None) -> Any:
        """Turn the public page on or off. Turning it on needs the environment's publishable key once (it is sealed and never returned)."""
        body: dict[str, Any] = {"public": public}
        if publishable_key is not None:
            body["publishable_key"] = publishable_key
        return self.put(seg("dashboards", dashboard_id, "publish"), body)
