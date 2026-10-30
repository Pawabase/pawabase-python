"""The management plane (``pb.manage``) and user administration (``pb.admin``): paths, bodies and query strings as the server defines them."""

import httpx
import pytest

from tests.conftest import make, make_async

BASE = "/platform/v1/envs/production"


@pytest.fixture
def sk(wire):
    wire.answer(httpx.Response(200, json={"data": []}))
    return make(wire, key="pb_sk_x", environment="production")


def test_every_service_is_bound_to_the_clients_environment(wire, sk):
    from pawabase.manage import SERVICES

    for name in SERVICES:
        assert getattr(sk.manage, name).env == "production"
    assert sk.manage.environment("staging").flows.env == "staging"


def test_definitions_use_names_and_branches(wire, sk):
    sk.manage.flows.list(branch="feat")
    assert wire.last.url.path == f"{BASE}/flows" and wire.query() == {"branch": "feat"}
    sk.manage.flows.replace("a b", {"name": "a b"})
    assert wire.last.method == "PUT" and wire.last.url.raw_path == f"{BASE}/flows/a%20b".encode()
    sk.manage.environment("staging").policies.remove("p")
    assert wire.last.method == "DELETE" and wire.last.url.path == "/platform/v1/envs/staging/policies/p"


def test_keys_and_secrets(wire, sk):
    sk.manage.keys.create("ci", role="secret", scopes=["routes:invoke"], allowed_ips=["10.0.0.0/8"])
    assert wire.last.url.path == f"{BASE}/keys" and wire.json() == {
        "name": "ci", "role": "secret", "scopes": ["routes:invoke"], "allowed_ips": ["10.0.0.0/8"], "allowed_routes": [],
    }
    sk.manage.keys.rotate("k1", grace_hours=2)
    assert wire.last.url.path == f"{BASE}/keys/k1/rotate" and wire.json() == {"grace_hours": 2}
    sk.manage.secrets.set("STRIPE_KEY", "v", rotate_every_days=30)
    assert wire.last.method == "PUT" and wire.json() == {"value": "v", "description": "", "rotate_every_days": 30}
    sk.manage.secrets.rollback("STRIPE_KEY")
    assert wire.last.url.path == f"{BASE}/secrets/STRIPE_KEY/rollback"


def test_firewall_rules_bans_and_the_dry_run(wire, sk):
    sk.manage.firewall.add_rule(name="Burst", action="rate_limit", conditions=[{"field": "path", "op": "starts_with", "value": "/auth"}], rate_limit={"requests": 5, "window_seconds": 60})
    body = wire.json()
    assert wire.last.url.path == f"{BASE}/firewall" and body["action"] == "rate_limit" and body["rate_limit"]["requests"] == 5 and body["logic"] == "all"
    sk.manage.firewall.order(["a", "b"])
    assert wire.last.method == "PUT" and wire.json() == {"ids": ["a", "b"]}
    sk.manage.firewall.ban("1.2.3.4", seconds=60)
    assert wire.json() == {"ip": "1.2.3.4", "seconds": 60, "reason": ""}
    sk.manage.firewall.test("1.2.3.4", path="/x")
    assert wire.last.url.path == f"{BASE}/firewall-test" and wire.json()["path"] == "/x"
    sk.manage.firewall.bans(include_expired=True)
    assert wire.query() == {"all": "1"}


def test_jobs_cache_and_schedules_send_what_the_server_reads(wire, sk):
    sk.manage.jobs.replay(job="send_mail", since_minutes=30, dry_run=True)
    assert wire.last.url.path == f"{BASE}/jobs/replay" and wire.json() == {"status": "failed", "job": "send_mail", "since_minutes": 30, "limit": 200, "dry_run": True, "ids": []}
    sk.manage.cache.invalidate(tags=["resource:posts"])
    assert wire.json() == {"tags": ["resource:posts"], "resources": []}


def test_observability_alarms_and_audit(wire, sk):
    sk.manage.observability.logs(q="timeout", minutes=15)
    assert wire.last.url.path == f"{BASE}/logs" and wire.query() == {"q": "timeout", "minutes": "15", "limit": "200"}
    sk.manage.observability.metric_series("errors", compare_previous=True)
    assert wire.query()["compare"] == "previous"
    sk.manage.alarms.create(name="5xx", metric="errors", threshold=10, recipients=["ops@x.io"])
    assert wire.json()["comparison"] == "gt" and wire.json()["recipients"] == ["ops@x.io"]
    sk.manage.alarms.history()
    assert wire.last.url.path == f"{BASE}/alarm-events"
    sk.manage.health.audit(actor="ada")
    assert wire.last.url.path == "/platform/v1/audit" and wire.query() == {"actor": "ada"}


def test_lifecycle_calls_are_project_wide_or_environment_scoped(wire, sk):
    sk.manage.environments.list()
    assert wire.last.url.path == "/platform/v1/envs"
    sk.manage.environments.update("staging", settings={"a": 1})
    assert wire.last.method == "PATCH" and wire.last.url.path == "/platform/v1/envs/staging" and wire.json() == {"settings": {"a": 1}}
    sk.manage.environments.promote("staging", include=["flows"])
    assert wire.last.url.path == f"{BASE}/promote" and wire.json() == {"to": "staging", "include": ["flows"]}
    sk.manage.backups.restore_snapshot("s1", strategy="replace", confirm="production")
    assert wire.last.url.path == f"{BASE}/snapshots/s1/restore" and wire.json()["confirm"] == "production" and wire.json()["safety_snapshot"] is True
    sk.manage.releases.merge("feat")
    assert wire.json() == {"target": "main", "force": False}


def test_user_administration_is_environment_scoped(wire, sk):
    sk.admin.users.update("u1", disabled=True)
    assert wire.last.method == "PATCH" and wire.last.url.path == f"{BASE}/auth/users/u1" and wire.json() == {"disabled": True}
    sk.admin.users.create(email="a@b.io", password="pw", roles=["editor"])
    assert wire.json()["roles"] == ["editor"] and wire.json()["email_verified"] is True
    sk.admin.users.revoke("u1", ["posts:write"])
    assert wire.last.method == "DELETE" and wire.json() == {"permissions": ["posts:write"]}
    sk.admin.roles.put("editor", ["posts:*"])
    assert wire.last.method == "PUT" and wire.last.url.path == f"{BASE}/auth/roles"
    sk.admin.orgs.add_member("acme", email="a@b.io", role="admin")
    assert wire.last.url.path == f"{BASE}/auth/orgs/acme/members"
    sk.admin.environment("staging").users.search(status="locked")
    assert wire.last.url.path == "/platform/v1/envs/staging/auth/users" and wire.query()["status"] == "locked"
    sk.admin.users.activity("u1", weeks=10)
    assert wire.last.method == "GET" and wire.last.url.path == f"{BASE}/auth/users/u1/activity" and wire.query() == {"weeks": "10"}
    sk.admin.users.timeline("u1", type="account", before="2026-10-30T10:00:00+00:00")
    assert wire.last.url.path == f"{BASE}/auth/users/u1/timeline" and wire.query() == {"type": "account", "limit": "50", "before": "2026-10-30T10:00:00+00:00"}
    sk.admin.insight.events(failed=True)
    assert wire.query() == {"failed": "true", "limit": "100"}


async def test_the_same_methods_are_awaitable_on_the_async_client(wire):
    wire.answer(httpx.Response(200, json={"data": []}))
    client = make_async(wire, key="pb_sk_x", environment="production")
    await client.manage.secrets.list()
    await client.admin.users.search(search="ada")
    assert wire.last.url.path == f"{BASE}/auth/users"


def test_realtime_channel_rules(wire, sk):
    sk.manage.realtime_channels.list()
    assert wire.last.url.path == f"{BASE}/realtime-channels"
    sk.manage.realtime_channels.create("chat:*", kind="presence", publish="signed_in", history=100)
    assert wire.last.method == "POST" and wire.json() == {
        "pattern": "chat:*", "kind": "presence", "subscribe": "authenticated", "publish": "signed_in", "presence": True, "history": 100,
        "message_size_limit": 0, "publish_rate_limit": 0, "subscription_limit": 0, "retention_seconds": 0, "description": "",
    }
    sk.manage.realtime_channels.update("c1", history=10, presence=False)
    assert wire.last.method == "PATCH" and wire.last.url.path == f"{BASE}/realtime-channels/c1" and wire.json() == {"history": 10, "presence": False}
    sk.manage.realtime_channels.remove("c1")
    assert wire.last.method == "DELETE" and wire.last.url.path == f"{BASE}/realtime-channels/c1"


def test_dashboards(wire, sk):
    sk.manage.dashboards.create("sales", {"pages": [{"title": "Sales"}]}, title="Sales")
    assert wire.last.method == "POST" and wire.last.url.path == f"{BASE}/dashboards"
    assert wire.json() == {"name": "sales", "title": "Sales", "description": "", "definition": {"pages": [{"title": "Sales"}]}, "enabled": True}
    sk.manage.dashboards.update("d1", enabled=False)
    assert wire.last.method == "PATCH" and wire.json() == {"enabled": False}
    sk.manage.dashboards.publish("d1", public=True, publishable_key="pb_pk_12345678")
    assert wire.last.method == "PUT" and wire.last.url.path == f"{BASE}/dashboards/d1/publish" and wire.json() == {"public": True, "publishable_key": "pb_pk_12345678"}
    sk.manage.dashboards.publish("d1", public=False)
    assert wire.json() == {"public": False}
    sk.manage.dashboards.get_one("d1")
    sk.manage.dashboards.remove("d1")
    assert wire.last.method == "DELETE"


def test_key_scopes_and_token_profile_preview(wire, sk):
    sk.manage.keys.scopes()
    assert wire.last.url.path == "/platform/v1/key-scopes"
    sk.admin.insight.preview_token_profile({"name": "billing"}, "u1", org="acme")
    assert wire.last.method == "POST" and wire.last.url.path == f"{BASE}/auth/token-profiles/preview"
    assert wire.json() == {"profile": {"name": "billing"}, "user_id": "u1", "org": "acme"}


def test_privacy_requests_export_erase_and_list(wire, sk):
    sk.admin.users.export_data("u1")
    assert wire.last.method == "POST" and wire.last.url.path == f"{BASE}/auth/users/u1/privacy/export"
    sk.admin.users.erase_data("u1", confirm="a@b.io")
    assert wire.last.method == "POST" and wire.last.url.path == f"{BASE}/auth/users/u1/privacy/erase" and wire.json() == {"confirm": "a@b.io"}
    sk.admin.privacy.requests(limit=5)
    assert wire.last.method == "GET" and wire.last.url.path == f"{BASE}/auth/privacy/requests" and wire.query() == {"limit": "5"}
    assert sk.admin.environment("staging").privacy.env == "staging"
