"""Observe: requests, logs, metrics, alarms, the status page, service health, plan usage and the audit trail."""

from __future__ import annotations

from datetime import datetime
from collections.abc import Sequence
from typing import Any

from ._base import Service, seg


def _iso(value: datetime | str | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


class Observability(Service):
    def overview(self, *, range: str | None = None) -> Any:
        """The environment at a glance: traffic, errors, latency, workers, storage."""
        return self.get("/overview", range=range)

    def analytics(self, *, range: str | None = None, start: datetime | str | None = None, end: datetime | str | None = None) -> Any:
        """Traffic over a ``range`` (``1h``, ``24h``, ``7d``…) or between ``start`` and ``end``."""
        return self.get("/analytics", range=range, **{"from": _iso(start), "to": _iso(end)})

    def requests(self, *, status: str | None = None, method: str | None = None, search: str | None = None, request_id: str | None = None, limit: int | None = None) -> Any:
        """Recent requests. ``status`` is an exact code or a class (``5xx``); ``search`` matches the path."""
        return self.get("/requests", status=status, method=method, search=search, request_id=request_id, limit=limit)

    def request(self, request_id: str) -> Any:
        """One request: its logs, database calls, spans, and the traceback if it failed."""
        return self.get(seg("requests", request_id))

    def logs(self, *, q: str = "", minutes: int = 60, limit: int = 200, after: datetime | str | None = None) -> Any:
        """Search application and access logs. ``after`` continues from the last entry you saw."""
        return self.get("/logs", q=q or None, minutes=minutes, limit=limit, after=_iso(after))

    def errors(self, *, minutes: int | None = None) -> Any:
        return self.get("/observability/errors", minutes=minutes)

    def routes(self, *, minutes: int | None = None, limit: int | None = None, sort: str | None = None) -> Any:
        """Per-route traffic, latency and errors."""
        return self.get("/observability/routes", minutes=minutes, limit=limit, sort=sort)

    def route(self, method: str, route: str, *, minutes: int | None = None) -> Any:
        return self.get("/observability/routes/detail", method=method, route=route, minutes=minutes)

    def system(self, *, minutes: int | None = None) -> Any:
        """CPU, memory and connection figures over time."""
        return self.get("/observability/system", minutes=minutes)

    def advisor(self, *, minutes: int | None = None) -> Any:
        """Findings the platform spotted: slow routes, missing indexes, noisy errors."""
        return self.get("/advisor", minutes=minutes)

    def metrics(self, *, name: str | None = None, minutes: int | None = None) -> Any:
        """Counters per minute."""
        return self.get("/metrics", name=name, minutes=minutes)

    def metric_catalog(self) -> Any:
        """The metrics you can chart or alarm on."""
        return self.get("/metric-catalog")

    def metric_series(self, metric: str = "requests", *, minutes: int = 60, period: int | None = None, compare_previous: bool = False) -> Any:
        """One metric over time; ``compare_previous`` adds the span before it."""
        return self.get("/metric-series", metric=metric, minutes=minutes, period=period, compare="previous" if compare_previous else None)

    def workers(self) -> Any:
        return self.get("/workers")

    def configuration(self) -> Any:
        """What the environment is running with."""
        return self.get("/configuration")

    def capacity(self) -> Any:
        return self.get("/capacity")


class Alarms(Service):
    """Alarms on a metric: when it crosses ``threshold`` for ``breaches_needed`` checks in a row, ``recipients`` are mailed."""

    def list(self) -> Any:
        return self.get("/alarms")

    def create(
        self, *, name: str, metric: str, threshold: float, comparison: str = "gt", window_minutes: int = 5, breaches_needed: int = 1,
        recipients: Sequence[str] = (), description: str = "", enabled: bool = True, renotify_minutes: int = 0,
    ) -> Any:
        """``comparison``: ``gt``, ``gte``, ``lt``, ``lte``. ``metric`` is a name from ``observability.metric_catalog()``."""
        return self.post("/alarms", {
            "name": name, "metric": metric, "threshold": threshold, "comparison": comparison, "window_minutes": window_minutes,
            "breaches_needed": breaches_needed, "recipients": list(recipients), "description": description, "enabled": enabled,
            "renotify_minutes": renotify_minutes,
        })

    def update(self, alarm_id: str, **changes: Any) -> Any:
        """Change some fields (the names of :meth:`create`); ``mute_minutes=60`` silences it for an hour, ``0`` un-mutes."""
        return self.patch(seg("alarms", alarm_id), changes)

    def remove(self, alarm_id: str) -> Any:
        return self.delete(seg("alarms", alarm_id))

    def check(self, alarm_id: str) -> Any:
        """Evaluate it now."""
        return self.post(seg("alarms", alarm_id, "check"))

    def test(self, alarm_id: str) -> Any:
        """Send the notification mail now."""
        return self.post(seg("alarms", alarm_id, "test"))

    def history(self, alarm_id: str | None = None) -> Any:
        """When alarms fired and recovered: one alarm's, or all."""
        return self.get(seg("alarms", alarm_id, "events")) if alarm_id else self.get("/alarm-events")


class StatusPage(Service):
    """The public status page and its incidents."""

    def get_page(self) -> Any:
        return self.get("/status-page")

    def configure(
        self, *, enabled: bool = True, title: str = "Status", description: str = "", contact_url: str = "", components: Sequence[dict[str, Any]] = (),
    ) -> Any:
        """``components``: ``{name, source, description}``; ``source`` is ``gateway``, ``api``, ``auth``, ``realtime``, ``workers`` (measured) or ``manual``."""
        return self.put("/status-page", {"enabled": enabled, "title": title, "description": description, "contact_url": contact_url, "components": list(components)})

    def incidents(self) -> Any:
        return self.get("/incidents")

    def open_incident(
        self, title: str, *, message: str = "", status: str = "investigating", impact: str = "minor", components: Sequence[str] = (),
    ) -> Any:
        """``status``: investigating, identified, monitoring, resolved. ``impact``: none, minor, major, critical."""
        return self.post("/incidents", {"title": title, "message": message, "status": status, "impact": impact, "components": list(components)})

    def update_incident(self, incident_id: str, message: str, *, status: str, impact: str | None = None) -> Any:
        body: dict[str, Any] = {"status": status, "message": message}
        if impact:
            body["impact"] = impact
        return self.post(seg("incidents", incident_id, "updates"), body)

    def remove_incident(self, incident_id: str) -> Any:
        return self.delete(seg("incidents", incident_id))


class Health(Service):
    """Service health, plan usage and the platform's own audit trail."""

    def services(self) -> Any:
        """Each service (database, cache, queue, storage, mail…) with its state, latency and error rate."""
        return self.get("/service-health")

    def usage(self) -> Any:
        """Each plan limit and how much of it is used."""
        return self.get("/plan-usage")

    def retry_policies(self) -> Any:
        return self.get("/retry-policies")

    def audit(
        self, *, action: str | None = None, actor: str | None = None, target: str | None = None, since: datetime | str | None = None,
        until: datetime | str | None = None, env: str | None = None,
    ) -> Any:
        """Management-plane changes: who did what, to what, when."""
        return self._c._call("GET", "/platform/v1/audit", params={k: v for k, v in {"action": action, "actor": actor, "target": target, "since": _iso(since), "until": _iso(until), "env": env}.items() if v is not None} or None, retry=True)

    def export_audit(self, *, format: str = "jsonl", **filters: Any) -> Any:
        """The audit trail as a file's contents (``jsonl`` or ``csv``), for the same filters as :meth:`audit`. Returns the HTTP response: read ``.text`` or ``.content``."""
        params = {"format": format, **{k: _iso(v) for k, v in filters.items() if v is not None}}
        return self._c._call("GET", "/platform/v1/audit/export", params=params, retry=True, raw=True)
