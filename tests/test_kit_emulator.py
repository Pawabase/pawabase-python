"""``pawabase emulate`` against a fake deployment: what runs locally, what is forwarded, and how triggers decide where code runs."""

import json
import textwrap

import httpx
import pytest

from pawabase import codec
from pawabase.client import AsyncPawabase
from pawabase.config import Settings
from pawabase.emulator import Emulator

ENV = "/platform/v1/projects/shop/envs/development"

CODE = textwrap.dedent(
    '''
    from pawabase.functions import FunctionError, function

    @function("greet", policy="public", input_fields=[{"name": "name", "type": "string"}])
    async def greet(ctx):
        ctx.log("greeting")
        return {"hello": ctx.input.get("name") or "world", "user": ctx.auth.get("user_id"), "trigger": ctx.trigger}

    @function("orders.show", policy="authenticated")
    async def show(ctx):
        order = await ctx.runtime.resource_get("orders", ctx.input["id"])
        if order is None:
            raise FunctionError("No such order.", status=404, code="not_found")
        return {"order": order, "q": ctx.request["query"], "ip": ctx.request["client_ip"], "headers": ctx.request["headers"]}

    @function("on-paid")
    async def on_paid(ctx):
        await ctx.runtime.emit("receipt.sent", {"order": ctx.input["event"]["payload"]["id"]})
        return {"handled": ctx.input["event"]["name"]}

    @function("nightly")
    async def nightly(ctx):
        return {"swept": True}

    @function("chain")
    async def chain(ctx):
        return {"inner": await ctx.runtime.call_function("greet", {"name": "chained"})}
    '''
)


class Deployment:
    """A stand-in for the gateway + platform: records every call and answers like the real one."""

    def __init__(self):
        self.calls = []
        self.orders = {"7": {"id": "7", "total": 10}}
        self.tokens = {"good-token": {"authenticated": True, "user_id": "u1", "kind": "user", "roles": [], "permissions": []}}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, body = request.url.path, (json.loads(request.content) if request.content else None)
        self.calls.append((request.method, path, body, dict(request.headers)))
        if path == f"{ENV}/functions":
            return httpx.Response(200, json={"data": [{"name": "greet"}, {"name": "deployed-only"}]})
        if path == f"{ENV}/routes":
            return httpx.Response(200, json={"data": [
                {"method": "GET", "path": "/orders/{id}", "handler_type": "function", "handler": "orders.show", "policy": "authenticated", "enabled": True},
                {"method": "GET", "path": "/hello", "handler_type": "function", "handler": "deployed-only", "policy": "public", "enabled": True}]})
        if path == f"{ENV}/subscriptions":
            return httpx.Response(200, json={"data": [
                {"name": "paid-local", "event": "order.paid", "target_type": "function", "target": "on-paid"},
                {"name": "paid-deployed", "event": "order.*", "target_type": "function", "target": "deployed-only"},
                {"name": "paid-flow", "event": "order.paid", "target_type": "flow", "target": "notify-flow"},
                {"name": "off", "event": "order.paid", "target_type": "function", "target": "greet", "enabled": False}]})
        if path == f"{ENV}/schedules":
            return httpx.Response(200, json={"data": [{"name": "nightly", "target_type": "function", "target": "nightly", "payload": {}}, {"name": "weekly", "target_type": "flow", "target": "f"}]})
        if path == f"{ENV}/runtime/identify":
            return httpx.Response(200, json={"auth": self.tokens.get(body["token"], {"authenticated": False, "kind": "anonymous", "user_id": None})})
        if path == f"{ENV}/runtime/call":
            method, args = body["method"], codec.decode(body["args"])
            if method == "check_policy":
                policy, context = args
                allowed = policy == "public" or (policy == "authenticated" and context["auth"]["authenticated"])
                return httpx.Response(200, json={"result": allowed})
            if method == "resource_get":
                return httpx.Response(200, json={"result": codec.encode(self.orders.get(args[1]))})
            if method == "emit":
                return httpx.Response(200, json={"result": "evt-1"})
            return httpx.Response(200, json={"result": None})
        if path == f"{ENV}/events":
            return httpx.Response(200, json={"event_id": "remote-evt"})
        if path.startswith(f"{ENV}/flows/") and path.endswith("/run"):
            return httpx.Response(200, json={"status": "succeeded"})
        if path.startswith(f"{ENV}/functions/") and path.endswith("/invoke"):
            return httpx.Response(200, json={"status": "succeeded", "result": {"deployed": True}})
        if path.startswith(f"{ENV}/schedules/") and path.endswith("/run"):
            return httpx.Response(200, json={"last_status": "succeeded"})
        return httpx.Response(200, json={"forwarded": path}, headers={"x-upstream": "yes"})


@pytest.fixture
async def emu(tmp_path):
    (tmp_path / "functions").mkdir()
    (tmp_path / "functions" / "f.py").write_text(CODE)
    deployment = Deployment()
    settings = Settings(url="http://gateway.test", api_key="pb_sk_testkey000000000000", project="shop", environment="development", root=tmp_path)
    client = AsyncPawabase(settings.url, settings.api_key, project="shop")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(deployment), base_url=settings.url, headers=client.headers)
    emulator = Emulator(settings, client=client, log=None)
    emulator.proxy = httpx.AsyncClient(transport=httpx.MockTransport(deployment), base_url=settings.url)
    emulator.deployment = deployment
    info = await emulator.start()
    emulator.info = info
    yield emulator
    await emulator.aclose()


def body(response):
    return json.loads(response.body)


async def test_startup_reports_what_is_local_what_is_deployed_and_what_is_served_locally(emu):
    assert emu.info["local"] == ["chain", "greet", "nightly", "on-paid", "orders.show"] and emu.info["errors"] == []
    assert emu.info["deployed"] == ["greet", "deployed-only"]
    assert emu.info["routes_served_locally"] == 1  # /orders/{id} (orders.show is local); /hello is bound to a function we do not have


async def test_a_local_function_runs_here_with_the_verified_caller(emu):
    response = await emu.handle("POST", "/functions/v1/greet", {"authorization": "Bearer good-token", "apikey": "pb_pk_x"}, b'{"name": "Ada"}')
    assert response.status == 200 and body(response) == {"data": {"hello": "Ada", "user": "u1", "trigger": "http"}}
    anonymous = await emu.handle("POST", "/functions/v1/greet", {"authorization": "Bearer forged"}, b"{}")
    assert body(anonymous)["data"]["user"] is None  # a token the deployment does not vouch for is nobody
    # Nothing about the call itself was sent to the deployment except identifying the caller and the policy question.
    assert not any(c[1].endswith("/functions/greet/invoke") for c in emu.deployment.calls)


async def test_input_validation_and_function_errors_become_the_status_the_function_chose(emu):
    invalid = await emu.handle("POST", "/functions/v1/greet", {}, b'{"name": 5}')
    assert invalid.status == 422 and body(invalid)["error"] == "invalid"
    assert (await emu.handle("POST", "/functions/v1/greet", {}, b"{not json")).status == 400


async def test_a_route_bound_to_a_local_function_runs_locally_with_the_platforms_payload(emu):
    denied = await emu.handle("GET", "/rest/v1/orders/7", {}, b"")
    assert denied.status == 401  # the deployment answered the policy question: authenticated
    response = await emu.handle("GET", "/rest/v1/orders/7?expand=lines", {"authorization": "Bearer good-token", "apikey": "pb_pk_x", "cookie": "s=1", "x-thing": "yes"}, b"", client_ip="203.0.113.5")
    payload = body(response)
    assert response.status == 200 and payload["order"] == {"id": "7", "total": 10} and payload["q"] == {"expand": "lines"} and payload["ip"] == "203.0.113.5"
    assert payload["headers"]["x-thing"] == "yes" and "cookie" not in payload["headers"] and "apikey" not in payload["headers"]
    missing = await emu.handle("GET", "/rest/v1/orders/404", {"authorization": "Bearer good-token"}, b"")
    assert missing.status == 404 and body(missing) == {"error": "not_found", "message": "No such order."}


async def test_a_secret_key_bypasses_the_policy_as_it_does_on_the_platform(emu):
    response = await emu.handle("GET", "/rest/v1/orders/7", {"apikey": "pb_sk_serverkey000000000000"}, b"")
    assert response.status == 200


async def test_everything_else_is_forwarded_unchanged_and_the_answer_relayed(emu):
    response = await emu.handle("GET", "/rest/v1/items?limit=2", {"apikey": "pb_pk_x", "authorization": "Bearer good-token"}, b"", client_ip="198.51.100.1")
    assert response.status == 200 and body(response) == {"forwarded": "/rest/v1/items"} and response.headers["x-upstream"] == "yes"
    forwarded = emu.deployment.calls[-1]
    assert forwarded[3]["authorization"] == "Bearer good-token" and forwarded[3]["x-forwarded-for"] == "198.51.100.1"
    # A function the deployment has but you do not is the deployment's to run.
    assert (await emu.handle("POST", "/functions/v1/deployed-only", {}, b"{}")).headers["x-emulator"] == "forwarded"


async def test_local_functions_calling_each_other_use_the_local_copy(emu):
    result = body(await emu.handle("POST", "/functions/v1/chain", {"apikey": "pb_sk_serverkey000000000000"}, b""))
    assert result["data"]["inner"]["hello"] == "chained"


async def test_an_event_runs_each_subscriber_where_its_code_lives(emu):
    result = await emu.trigger_event("order.paid", {"id": 7})
    ran = {r["subscription"]: r["ran"] for r in result["results"]}
    assert ran == {"paid-local": "local", "paid-deployed": "deployment", "paid-flow": "deployment"}  # the disabled subscription did not run
    local = next(r for r in result["results"] if r["subscription"] == "paid-local")
    assert local["result"] == {"handled": "order.paid"}
    # The event itself was NOT emitted: that would run the deployed copy of a function that just ran here.
    assert not any(c[1] == f"{ENV}/events" for c in emu.deployment.calls)
    # The local function's own work went to the deployment (it emitted a follow-up event there).
    assert any(c[1] == f"{ENV}/runtime/call" and c[2]["method"] == "emit" and c[2]["args"][0] == "receipt.sent" for c in emu.deployment.calls)


async def test_a_remote_event_just_emits_on_the_deployment(emu):
    assert (await emu.trigger_event("order.paid", {"id": 7}, remote_only=True))["event_id"] == "remote-evt"


async def test_a_schedule_runs_locally_when_its_function_is_local_and_remotely_otherwise(emu):
    assert (await emu.trigger_schedule("nightly"))["ran"] == "local"
    assert (await emu.trigger_schedule("weekly"))["ran"] == "deployment"
    with pytest.raises(LookupError):
        await emu.trigger_schedule("nope")


async def test_control_endpoints_and_the_run_history(emu):
    await emu.handle("POST", "/functions/v1/greet", {}, b"{}")
    assert body(await emu.handle("GET", "/_emulator/health", {}, b""))["project"] == "shop"
    assert {f["name"] for f in body(await emu.handle("GET", "/_emulator/functions", {}, b""))["data"]} >= {"greet", "chain"}
    assert body(await emu.handle("GET", "/_emulator/routes", {}, b""))["data"][0]["path"] == "/rest/v1/orders/{id}"
    runs = body(await emu.handle("GET", "/_emulator/runs", {}, b""))["data"]
    assert runs[0]["function"] == "greet" and runs[0]["ok"] is True
    triggered = body(await emu.handle("POST", "/_emulator/trigger/schedule/nightly", {}, b""))
    assert triggered["ran"] == "local"
    assert (await emu.handle("GET", "/_emulator/nothing", {}, b"")).status == 404


async def test_the_emulator_survives_an_unreachable_deployment(emu):
    def down(request):
        raise httpx.ConnectError("refused")

    emu.proxy = httpx.AsyncClient(transport=httpx.MockTransport(down), base_url="http://gateway.test")
    response = await emu.handle("GET", "/rest/v1/items", {}, b"")
    assert response.status == 502 and body(response)["error"] == "deployment_unreachable"


async def test_a_rate_limited_runtime_call_waits_and_retries_instead_of_failing(emu):
    import pawabase.client as client_module

    calls = {"n": 0}
    inner = emu.client._client._transport

    def limited(request):
        if request.url.path.endswith("/runtime/call") and calls["n"] < 2:
            calls["n"] += 1
            return httpx.Response(429, json={"error": "rate_limit_exceeded", "retry_after": 1})
        return inner.handle_request(request)

    emu.client._client = httpx.AsyncClient(transport=httpx.MockTransport(limited), base_url="http://gateway.test", headers=emu.client.headers)
    original = client_module.rate_wait
    client_module.rate_wait = lambda response: 0.01
    try:
        response = await emu.handle("GET", "/rest/v1/orders/7", {"authorization": "Bearer good-token"}, b"")
    finally:
        client_module.rate_wait = original
    assert response.status == 200 and calls["n"] == 2


async def test_a_websocket_upgrade_is_refused_with_a_pointer_to_the_gateway(emu):
    response = await emu.handle("GET", "/realtime/v1/socket", {"upgrade": "websocket", "connection": "Upgrade"}, b"")
    assert response.status == 501 and "directly" in body(response)["message"] and "gateway.test" in body(response)["message"]
