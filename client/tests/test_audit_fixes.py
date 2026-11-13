"""Fixes from the docs audit: error codes, similar() filters, empty async lists, admin organization fields, mail, transforms."""

import httpx
import pytest

from pawabase import ForbiddenError
from tests.conftest import make, make_async

BASE = "/platform/v1/envs/production"


def test_an_error_code_in_a_code_field_is_read(wire, pb):
    wire.answer(httpx.Response(403, json={"code": "mfa_setup_required", "message": "set up a second factor", "mfa_token": "t", "methods": ["totp"]}))
    with pytest.raises(ForbiddenError) as raised:
        pb.auth.sign_in_with_password(email="a@b.c", password="p")
    assert raised.value.code == "mfa_setup_required"
    assert raised.value.detail["mfa_token"] == "t"


def test_similar_takes_where_conditions(wire, pb):
    wire.answer(httpx.Response(200, json={"data": [], "metric": "cosine", "count": 0}))
    pb.from_("notes").similar(text="refunds", where={"kind": "public", "archived": False, "rank": {"gte": 2}})
    assert wire.query() == {"filter[kind]": "eq.public", "filter[archived]": "is.false", "filter[rank]": "gte.2"}
    assert pb.from_("notes").similar(text="x", where={"id": {"in": []}}) == {"data": [], "metric": "cosine", "count": 0}
    assert len(wire.requests) == 1


async def test_an_empty_in_list_is_answered_on_the_async_client_too(wire):
    apb = make_async(wire)
    page = await apb.from_("notes").in_("id", []).execute()
    assert page.data == [] and page.total == 0
    empty = await apb.from_("notes").similar(text="x", where={"id": {"in": []}})
    assert empty["count"] == 0
    assert wire.requests == []


def test_admin_organization_update_sends_every_field(wire):
    sk = make(wire, key="pb_sk_x", environment="production")
    sk.admin.orgs.update("acme", app_metadata={"plan": "pro"}, max_members=None, require_mfa=True, suspended=False)
    assert wire.last.method == "PATCH" and wire.last.url.path == f"{BASE}/auth/orgs/acme"
    assert wire.json() == {"app_metadata": {"plan": "pro"}, "require_mfa": True, "suspended": False, "max_members": None}
    sk.admin.orgs.update("acme", name="Acme")
    assert wire.json() == {"name": "Acme"}


def test_mail_send_retry_cancel_and_stats(wire):
    sk = make(wire, key="pb_sk_x", environment="production")
    sk.manage.mail.send(to=["ada@example.com"], template="welcome", data={"name": "Ada"})
    assert wire.last.url.path == f"{BASE}/mail/send"
    assert wire.json()["to"] == ["ada@example.com"] and wire.json()["template"] == "welcome"
    sk.manage.mail.retry(7)
    assert wire.last.url.path == f"{BASE}/mail/log/7/retry"
    sk.manage.mail.cancel(8)
    assert wire.last.url.path == f"{BASE}/mail/log/8/cancel"
    sk.manage.mail.stats(days=7)
    assert wire.query() == {"days": "7"}


def test_transform_url_carries_rotate(pb):
    url = pb.storage.from_("avatars").transform_url("me.png", width=96, fit="cover", rotate=90)
    assert "rotate=90" in url and "fit=cover" in url
