"""Automation: flows, schedules, events and their subscriptions, outbound and inbound webhooks, and the job queue."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ._base import Definitions, Service, seg


class Flows(Definitions):
    """Flows: graphs of blocks that run on a trigger (HTTP, event, schedule, resource change, webhook, manual)."""

    kind = "flows"

    def run(self, name: str, input: Any = None, *, as_user: Mapping[str, Any] | None = None, entry: str | None = None) -> Any:
        """Run a flow now as an operator (or ``as_user``): ``{status, run_id, result, response, trace}``. ``entry`` picks a trigger node."""
        return self.post(seg(self.kind, name, "run"), {"input": input, "as_user": dict(as_user) if as_user else None, "entry": entry})

    def validate(self, definition: Mapping[str, Any]) -> Any:
        """Check a flow's graph without saving it: the problems, node by node."""
        return self._c._call("POST", "/platform/v1/flows/validate", json={"definition": dict(definition)})

    def blocks(self) -> Any:
        """The catalogue of blocks a flow can use, with their configuration fields."""
        return self._c._call("GET", "/platform/v1/blocks", retry=True)

    def runs(self, *, flow: str | None = None, status: str | None = None, limit: int | None = None, after: str | None = None) -> Any:
        return self.get("/flow-runs", flow=flow, status=status, limit=limit, after=after)

    def run_detail(self, run_id: str) -> Any:
        """One run with its per-block trace."""
        return self.get(seg("flow-runs", run_id))


class Schedules(Definitions):
    """Cron and interval schedules that run a flow or function, or emit an event."""

    kind = "schedules"

    def run(self, name: str) -> Any:
        """Fire a schedule now, exactly as the scheduler would."""
        return self.post(seg(self.kind, name, "run"))


class Subscriptions(Definitions):
    """Event subscriptions: which events run which flow or function, or reach which realtime channel."""

    kind = "subscriptions"


class Events(Service):
    """The event bus: emit, and inspect what happened to each event."""

    def emit(self, name: str, payload: Any = None) -> Any:
        """Publish an event: every subscription, flow and webhook listening for it runs."""
        return self.post("/events", {"name": name, "payload": payload})

    def list(self, *, name: str | None = None, limit: int | None = None, after: str | None = None) -> Any:
        return self.get("/events", name=name, limit=limit, after=after)

    def detail(self, event_id: str) -> Any:
        """One event with every consumer it reached and how that went."""
        return self.get(seg("events", event_id))

    def graph(self) -> Any:
        """Which event names exist and who consumes each."""
        return self.get("/events-graph")


class Webhooks(Definitions):
    """Outbound webhooks: endpoints that receive signed events, with retries."""

    kind = "webhooks"

    def test(self, name: str) -> Any:
        """Send one test event to an endpoint (one attempt, not retried)."""
        return self.post(seg(self.kind, name, "test"))

    def deliveries(self, *, status: str | None = None, endpoint: str | None = None, limit: int | None = None) -> Any:
        """Delivery history. ``status``: ``pending``, ``retrying``, ``delivered``, ``failed``."""
        return self.get("/webhook-deliveries", status=status, endpoint=endpoint, limit=limit)

    def redeliver(self, delivery_id: str) -> Any:
        return self.post(seg("webhook-deliveries", delivery_id, "redeliver"))

    def redeliver_many(
        self, *, ids: Sequence[str] | None = None, status: str = "failed", endpoint: str | None = None, event: str | None = None, since_minutes: int | None = None,
        limit: int = 100, dry_run: bool = False,
    ) -> Any:
        """Send many again: by ``ids``, or every delivery matching a filter (up to ``limit``). ``dry_run`` only counts."""
        body = {"ids": list(ids) if ids else None, "status": status, "endpoint": endpoint, "event": event, "since_minutes": since_minutes, "limit": limit, "dry_run": dry_run}
        return self.post("/webhook-deliveries/redeliver", {k: v for k, v in body.items() if v is not None})


class InboundHooks(Definitions):
    """Inbound hooks: URLs other services call (signature-checked), each starting a flow or function."""

    kind = "inbound-hooks"

    def url(self, slug: str) -> Any:
        """The public URL to give the sender."""
        return self.get(seg(self.kind, slug, "url"))


class Jobs(Service):
    """Background work: jobs, failures and replays, queues, workers and how failed work is retried."""

    def list(self, *, status: str | None = None, queue: str | None = None, limit: int | None = None) -> Any:
        """``status``: ``queued``, ``active``, ``retrying``, ``succeeded``, ``failed``."""
        return self.get("/jobs", status=status, queue=queue, limit=limit)

    def detail(self, job_id: str) -> Any:
        return self.get(seg("jobs", job_id))

    def retry(self, job_id: str) -> Any:
        return self.post(seg("jobs", job_id, "retry"))

    def replay(
        self, *, ids: Sequence[str] | None = None, status: str = "failed", queue: str | None = None, job: str | None = None, since_minutes: int | None = None,
        limit: int = 200, dry_run: bool = False,
    ) -> Any:
        """Queue recorded jobs again: named ``ids``, or every job matching the filter (failed by default). ``dry_run`` only counts."""
        body = {"ids": list(ids or []), "status": status, "queue": queue, "job": job, "since_minutes": since_minutes, "limit": limit, "dry_run": dry_run}
        return self.post("/jobs/replay", {k: v for k, v in body.items() if v is not None})

    def failed(self) -> Any:
        """Jobs that gave up after their retries."""
        return self.get("/failed-jobs")

    def dismiss_failed(self, job_id: str) -> Any:
        return self.delete(seg("failed-jobs", job_id))

    def queues(self) -> Any:
        """Queues with depth and job counts."""
        return self.get("/queues")

    def workers(self) -> Any:
        """The processes taking jobs (installation-wide)."""
        return self._c._call("GET", "/platform/v1/workers", retry=True)

    def retry_policies(self) -> Any:
        """How each kind of background work is retried: attempts and the wait before each retry."""
        return self._c._call("GET", "/platform/v1/retry-policies", retry=True)
