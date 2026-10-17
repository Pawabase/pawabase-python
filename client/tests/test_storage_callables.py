import httpx
import pytest

from pawabase import ConfigError
from tests.conftest import make, make_async


def test_upload_download_and_listing(wire, pb):
    wire.answer(httpx.Response(200, json={"bucket": "av", "key": "a/b c.png", "size": 3, "content_type": "image/png"}))
    stored = pb.storage.from_("av").upload("a/b c.png", b"abc", content_type="image/png")
    assert stored["size"] == 3 and wire.last.method == "PUT" and wire.last.url.raw_path == b"/storage/v1/object/av/a/b%20c.png"
    assert wire.last.headers["content-type"] == "image/png" and wire.last.content == b"abc"
    wire.answer(httpx.Response(200, content=b"abc"))
    assert pb.storage.from_("av").download("a/b c.png") == b"abc"
    wire.answer(httpx.Response(200, json={"files": [{"key": "x"}], "prefixes": [], "cursor": ""}))
    assert pb.storage.from_("av").list(prefix="a/", limit=5)["files"] == [{"key": "x"}] and wire.query() == {"prefix": "a/", "limit": "5"}


def test_list_all_follows_the_cursor(wire, pb):
    pages = [httpx.Response(200, json={"files": [{"key": "1"}], "prefixes": [], "cursor": "c1"}), httpx.Response(200, json={"files": [{"key": "2"}], "prefixes": [], "cursor": ""})]
    wire.answer(lambda request: pages.pop(0))
    assert [f["key"] for f in pb.storage.from_("av").list_all(page_size=1)] == ["1", "2"]


def test_urls_and_signed_links(wire, pb):
    bucket = pb.storage.from_("av")
    assert bucket.public_url("a b.png") == "http://gateway.test/storage/v1/files/development/av/a%20b.png"
    assert bucket.transform_url("a.png", width=200, fit="cover", format="webp") == (
        "http://gateway.test/storage/v1/files/development/av/a.png?width=200&fit=cover&format=webp"
    )
    wire.answer(httpx.Response(200, json={"url": "u", "expires_at": 1, "method": "GET"}))
    bucket.create_signed_url("a.png", expires_in=60)
    assert wire.json() == {"method": "GET", "expires_in": 60}
    bucket.create_signed_upload_url("in/x.png", max_bytes=10, content_type="image/png")
    assert wire.json() == {"method": "PUT", "expires_in": 300, "content_type": "image/png", "max_bytes": 10}


def test_share_links_need_a_secret_key(wire, pb):
    with pytest.raises(ConfigError):
        pb.storage.from_("av").create_share_link("a.png")
    admin = make(wire, key="pb_sk_x")
    wire.answer(httpx.Response(201, json={"id": "sh1"}))
    admin.storage.from_("av").create_share_link("a.png", expires_in=600, permissions="view")
    assert wire.last.url.path == "/platform/v1/envs/development/buckets/av/shares" and wire.json()["permissions"] == "view"


def test_functions_flows_and_routes(wire, pb):
    wire.answer(httpx.Response(200, json={"data": {"ok": True}}))
    assert pb.functions.invoke("orders.create", {"sku": "A1"}, branch="feat") == {"ok": True}
    assert wire.last.url.path == "/functions/v1/orders.create" and wire.query() == {"branch": "feat"} and wire.json() == {"sku": "A1"}
    wire.answer(httpx.Response(200, json={"sum": 42}))
    assert pb.flows.invoke("demo", {"a": 1}) == {"sum": 42} and wire.last.url.path == "/flows/v1/demo"
    pb.routes.post("orders/42/pay", {"m": "card"})
    assert wire.last.url.path == "/rest/v1/orders/42/pay" and wire.last.method == "POST"


async def test_realtime_over_http_and_the_async_only_channel(wire):
    from pawabase import Pawabase

    blocking = make(wire)
    wire.answer(httpx.Response(200, json={"id": "m1", "channel": "c", "event": "e"}))
    blocking.realtime.publish("c", "e", {"x": 1})
    assert wire.json() == {"channel": "c", "event": "e", "payload": {"x": 1}}
    with pytest.raises(ConfigError, match="async"):
        blocking.realtime.channel("c")
    wire.answer(httpx.Response(200, json={"messages": [{"id": "1"}]}))
    assert await make_async(wire).realtime.history("c", 5) == [{"id": "1"}]
    assert isinstance(blocking, Pawabase)


def test_http_functions_are_called_with_any_method_and_path_and_errors_are_returned(wire, pb):
    wire.answer(httpx.Response(404, json={"error": "not_found"}))
    r = pb.functions.request("orders", "/42/items", method="put", json={"sku": "A1"}, params={"q": "x"}, branch="feat")
    assert r.status_code == 404 and r.json() == {"error": "not_found"}
    assert wire.last.method == "PUT" and wire.last.url.path == "/functions/v1/orders/42/items" and wire.query() == {"q": "x"}
    assert wire.last.headers["x-pawabase-branch"] == "feat" and wire.json() == {"sku": "A1"}
    wire.answer(httpx.Response(200, text="hi"))
    assert pb.functions.request("orders").text == "hi" and wire.last.method == "GET" and wire.last.url.path == "/functions/v1/orders"


async def test_http_functions_on_the_async_client(wire):
    wire.answer(httpx.Response(201, json={"ok": True}))
    r = await make_async(wire).functions.request("hook", content="raw")
    assert r.status_code == 201 and wire.last.method == "POST" and wire.last.content == b"raw"
