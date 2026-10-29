"""The SDK against a real Pawabase gateway. Skipped unless these are set:

    PAWABASE_LIVE_URL=http://localhost:8080  PAWABASE_LIVE_ENV=demo-ts-dev
    PAWABASE_LIVE_PUBLISHABLE=pb_pk_…         PAWABASE_LIVE_SECRET=pb_sk_…

It creates a resource and a bucket of its own (``sdk_live_*``) and removes them afterwards.
"""

import os
import uuid

import pytest

from pawabase import AsyncPawabase, ConflictError, NotFoundError, Pawabase

URL = os.environ.get("PAWABASE_LIVE_URL", "")
ENV = os.environ.get("PAWABASE_LIVE_ENV", "development")
PUBLISHABLE = os.environ.get("PAWABASE_LIVE_PUBLISHABLE", "")
SECRET = os.environ.get("PAWABASE_LIVE_SECRET", "")
pytestmark = pytest.mark.skipif(not (URL and PUBLISHABLE and SECRET), reason="needs a running gateway (PAWABASE_LIVE_*)")

RESOURCE = "sdk_live_" + uuid.uuid4().hex[:6]
BUCKET = "sdk-live-" + uuid.uuid4().hex[:6]


@pytest.fixture(scope="module")
def admin():
    with Pawabase(URL, SECRET, environment=ENV) as client:
        client.manage.resources.create(
            {
                "name": RESOURCE,
                "fields": [
                    {"name": "title", "type": "string", "required": True, "unique": True},
                    {"name": "views", "type": "integer", "default": 0},
                    {"name": "done", "type": "boolean", "default": False},
                ],
                "operations": {op: {"enabled": True, "policy": "public"} for op in ("list", "get", "create", "update", "delete")},
            }
        )
        client.manage.resources.migrate(RESOURCE)
        client.manage.buckets.create({"name": BUCKET, "public": True, "write_policy": "public", "signed_downloads": True})
        try:
            yield client
        finally:
            client.manage.resources.remove(RESOURCE)
            client.manage.buckets.remove(BUCKET)


@pytest.fixture
def pb(admin):
    with Pawabase(URL, PUBLISHABLE, environment=ENV) as client:
        yield client


def test_records_end_to_end(pb):
    items = pb.from_(RESOURCE)
    first = items.create(title="one", views=3)
    items.create(title="two", views=9, done=True)
    assert items.get(first["id"])["title"] == "one"
    assert items.update(first["id"], views=4)["views"] == 4
    assert [r["title"] for r in items.gte("views", 5).execute().data] == ["two"]
    assert [r["title"] for r in items.eq("done", True).execute().data] == ["two"]  # booleans go through `is`
    assert [r["title"] for r in items.eq("done", False).execute().data] == ["one"]
    assert [r["title"] for r in items.order("-views").all()] == ["two", "one"]
    assert items.count() == 2 and items.eq("title", "one").exists()
    with pytest.raises(ConflictError):
        items.create(title="one")
    items.delete(first["id"])
    with pytest.raises(NotFoundError):
        items.get(first["id"])


def test_auth_end_to_end(pb):
    email = f"sdk-{uuid.uuid4().hex[:8]}@example.com"
    outcome = pb.auth.sign_up(email=email, password="Correct-horse-9", name="Ada")
    if outcome.verification_required:
        pytest.skip("this environment requires email verification")
    assert pb.auth.user["email"] == email
    assert pb.auth.sign_in_with_password(email=email, password="Correct-horse-9").status == "signed_in"
    assert pb.auth.get_user()["email"] == email
    assert pb.auth.update_user(name="Ada L.")["name"] == "Ada L."
    assert pb.auth.sessions.list()
    pb.auth.refresh_session()
    pb.auth.sign_out()
    assert pb.auth.session is None


def test_storage_end_to_end(pb):
    bucket = pb.storage.from_(BUCKET)
    stored = bucket.upload("notes/a b.txt", b"hello", content_type="text/plain")
    assert stored["size"] == 5
    assert bucket.download("notes/a b.txt") == b"hello"
    assert [f["key"] for f in bucket.list(prefix="notes/")["files"]] == ["notes/a b.txt"]
    assert BUCKET in [b["name"] for b in pb.storage.list_buckets()]
    signed = bucket.create_signed_url("notes/a b.txt", expires_in=60)
    assert signed["url"] and signed["method"] == "GET"
    bucket.remove("notes/a b.txt")
    with pytest.raises(NotFoundError):
        bucket.download("notes/a b.txt")


def test_functions_and_flows_and_the_management_plane(pb, admin):
    functions = [f["name"] for f in admin.manage.functions.list()["data"]]
    if "greet.hello" in functions:
        assert pb.functions.invoke("greet.hello", name="Ada") == {"message": "Hello, Ada!"}
    assert admin.manage.resources.list()["data"]
    assert admin.manage.flows.list()["data"] is not None
    assert isinstance(admin.admin.users.search()["data"], list)
    assert admin.manage.observability.overview()


async def test_the_async_client_and_live_realtime(admin):
    async with AsyncPawabase(URL, PUBLISHABLE, environment=ENV) as apb:
        created = await apb.from_(RESOURCE).create(title="async")
        assert (await apb.from_(RESOURCE).eq("title", "async").single())["id"] == created["id"]
        assert await apb.from_(RESOURCE).count() >= 1
        status = await apb.status()
        assert status
    admin.manage.resources.delete_record(RESOURCE, created["id"])
