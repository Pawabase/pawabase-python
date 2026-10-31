"""``pb.manage``: the management plane of one environment, with that environment's secret key (``pb_sk_…``).

    pb = Pawabase(url, "pb_sk_…", environment="production")
    pb.manage.secrets.set("STRIPE_KEY", "sk_live_…")
    pb.manage.firewall.ban("203.0.113.9", seconds=3600)
    pb.manage.environment("staging").flows.list()       # the same API for another environment the key may manage
"""

from __future__ import annotations

from typing import Any

from .access import Domains, Firewall, Keys, Secrets
from .ai import Ai
from .automation import Events, Flows, InboundHooks, Jobs, Schedules, Subscriptions, Webhooks
from .data import Database, Policies, Resources, Routes, Runtime, Schema, Schemas, Transformers
from .files import Buckets, Cache
from .lifecycle import Backups, Environments, Releases
from .mail import Mail, MailTemplates
from .observe import Alarms, Health, Observability, StatusPage
from .realtime import Dashboards, RealtimeChannels

__all__ = ["Manage"]

SERVICES: dict[str, type] = {
    # data
    "resources": Resources, "schema": Schema, "policies": Policies, "schemas": Schemas, "transformers": Transformers, "routes": Routes,
    "database": Database, "runtime": Runtime,
    # automation
    "flows": Flows, "schedules": Schedules, "subscriptions": Subscriptions, "events": Events, "webhooks": Webhooks,
    "inbound_hooks": InboundHooks, "jobs": Jobs,
    # files and mail
    "buckets": Buckets, "cache": Cache, "mail": Mail, "mail_templates": MailTemplates,
    # realtime and dashboards
    "realtime_channels": RealtimeChannels, "dashboards": Dashboards,
    # access
    "keys": Keys, "secrets": Secrets, "domains": Domains, "firewall": Firewall,
    # observe
    "observability": Observability, "alarms": Alarms, "status_page": StatusPage, "health": Health,
    # ai
    "ai": Ai,
    # lifecycle
    "environments": Environments, "releases": Releases, "backups": Backups,
}


class Manage:
    """Every management service as an attribute. Built once per client; :meth:`environment` re-binds them to another environment."""

    resources: Resources
    schema: Schema
    policies: Policies
    schemas: Schemas
    transformers: Transformers
    routes: Routes
    database: Database
    runtime: Runtime
    flows: Flows
    schedules: Schedules
    subscriptions: Subscriptions
    events: Events
    webhooks: Webhooks
    inbound_hooks: InboundHooks
    jobs: Jobs
    buckets: Buckets
    cache: Cache
    mail: Mail
    mail_templates: MailTemplates
    realtime_channels: RealtimeChannels
    dashboards: Dashboards
    keys: Keys
    secrets: Secrets
    domains: Domains
    firewall: Firewall
    observability: Observability
    alarms: Alarms
    status_page: StatusPage
    health: Health
    ai: Ai
    environments: Environments
    releases: Releases
    backups: Backups

    def __init__(self, client: Any, environment: str | None = None) -> None:
        self._client = client
        self._environment = environment
        for name, service in SERVICES.items():
            setattr(self, name, service(client, environment))

    def environment(self, name: str) -> Manage:
        """The same services for environment *name*."""
        return Manage(self._client, name)
