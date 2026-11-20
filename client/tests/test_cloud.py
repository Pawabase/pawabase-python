import asyncio
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from pawabase.cloud import AsyncCloud, AsyncOAuth, Cloud, CloudError, OAuth, OAuthError, OAuthTokens, pkce_challenge

APP = {"client_id": "pbc_abc", "client_secret": "pbs_secret", "redirect_uri": "https://app.example.com/cb"}
REPLY = {"access_token": "at1", "refresh_token": "pbr_1", "expires_in": 3600, "scope": "projects:read projects:write", "organization": "acme"}


def serve(answer):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return answer(request, len(calls) - 1)

    return httpx.MockTransport(handler), calls


def form(request):
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def test_authorize_is_pkce_s256_and_the_challenge_matches_the_verifier():
    pending = OAuth(**APP).authorize(scopes=["projects:read", "projects:write"])
    query = {k: v[0] for k, v in parse_qs(urlparse(pending.url).query).items()}
    assert pending.url.startswith("https://dashboard.pawabase.com/oauth/authorize?")
    assert query["scope"] == "projects:read projects:write" and query["state"] == pending.state
    assert query["code_challenge_method"] == "S256"
    expected = base64.urlsafe_b64encode(hashlib.sha256(pending.code_verifier.encode()).digest()).decode().rstrip("=")
    assert query["code_challenge"] == expected == pkce_challenge(pending.code_verifier)
    assert len(pending.code_verifier) >= 43


def test_callback_refuses_a_return_it_did_not_start_a_declined_one_and_a_missing_code():
    oauth = OAuth(**APP)
    with pytest.raises(OAuthError) as wrong:
        oauth.callback("https://app.example.com/cb?code=c&state=other", state="s1", code_verifier="v")
    assert wrong.value.code == "invalid_state"
    with pytest.raises(OAuthError) as declined:
        oauth.callback({"error": "access_denied", "error_description": "The person declined.", "state": "s1"}, state="s1", code_verifier="v")
    assert declined.value.code == "access_denied" and "declined" in str(declined.value)
    with pytest.raises(OAuthError) as none:
        oauth.callback("https://app.example.com/cb?state=s1", state="s1", code_verifier="v")
    assert none.value.code == "invalid_request"


def test_callback_trades_the_code_with_basic_auth_the_verifier_and_the_redirect():
    transport, calls = serve(lambda r, i: httpx.Response(200, json=REPLY))
    tokens = OAuth(**APP, transport=transport).callback({"code": "pbo_1", "state": "s1"}, state="s1", code_verifier="verifier")
    sent = calls[0]
    assert str(sent.url) == "https://dashboard.pawabase.com/oauth/token"
    assert sent.headers["authorization"] == "Basic " + base64.b64encode(b"pbc_abc:pbs_secret").decode()
    assert form(sent) == {"grant_type": "authorization_code", "code": "pbo_1", "redirect_uri": "https://app.example.com/cb", "code_verifier": "verifier"}
    assert (tokens.access_token, tokens.refresh_token, tokens.organization) == ("at1", "pbr_1", "acme")
    assert tokens.scope == ["projects:read", "projects:write"] and tokens.expires_at > time.time() + 3000


def test_a_public_client_sends_its_id_in_the_body_and_no_secret():
    transport, calls = serve(lambda r, i: httpx.Response(200, json=REPLY))
    OAuth(client_id="pbc_pub", redirect_uri="http://127.0.0.1:9/cb", transport=transport).exchange_code("c", code_verifier="v")
    assert "authorization" not in calls[0].headers and form(calls[0])["client_id"] == "pbc_pub"


def test_oauth_refusals_become_oauth_errors():
    transport, _ = serve(lambda r, i: httpx.Response(400, json={"error": "invalid_grant", "error_description": "The refresh token was revoked."}))
    with pytest.raises(OAuthError) as gone:
        OAuth(**APP, transport=transport).refresh("pbr_old")
    assert gone.value.code == "invalid_grant" and gone.value.needs_reconnect and gone.value.status == 400
    slow, _ = serve(lambda r, i: httpx.Response(429, json={"error": "slow_down", "error_description": "Too many."}, headers={"retry-after": "12"}))
    with pytest.raises(OAuthError) as limited:
        OAuth(**APP, transport=slow).refresh("x")
    assert limited.value.code == "slow_down" and limited.value.retry_after == 12


def test_client_credentials_and_revoke():
    transport, calls = serve(lambda r, i: httpx.Response(200, json={"access_token": "platform", "expires_in": 3600, "scope": "projects:write"} if "token" in r.url.path else {}))
    oauth = OAuth(**APP, transport=transport)
    tokens = oauth.client_credentials(["projects:write"])
    assert form(calls[0])["grant_type"] == "client_credentials" and tokens.refresh_token is None
    oauth.revoke("pbr_1")
    assert calls[1].url.path == "/oauth/revoke" and form(calls[1])["token"] == "pbr_1"


def test_token_source_returns_the_token_while_fresh_and_refreshes_once_then_saves():
    transport, calls = serve(lambda r, i: httpx.Response(200, json={**REPLY, "access_token": "at2", "refresh_token": "pbr_2"}))
    oauth = OAuth(**APP, transport=transport)
    saved = []
    stale = OAuthTokens("at1", "pbr_1", time.time() - 10, [], "acme")
    token = oauth.token_source(stale, on_refresh=lambda t: saved.append(t.refresh_token))
    assert token() == "at2" and token() == "at2"
    assert len(calls) == 1 and saved == ["pbr_2"]


def test_tokens_round_trip_through_a_dict_for_storage():
    tokens = OAuthTokens("a", "r", 123.0, ["projects:read"], "acme")
    assert OAuthTokens.from_dict(json.loads(json.dumps(tokens.to_dict()))) == tokens


def test_management_api_sends_the_bearer_and_errors_carry_code_and_details():
    transport, calls = serve(lambda r, i: httpx.Response(200, json=[{"ref": "p1", "state": "running"}]) if r.method == "GET" else httpx.Response(403, json={"error": "insufficient_scope", "message": "Needs projects:write.", "details": {"needed": "projects:write"}}))
    cloud = Cloud(token="pbp_x", transport=transport)
    assert cloud.projects.list()[0]["ref"] == "p1"
    assert calls[0].headers["authorization"] == "Bearer pbp_x" and str(calls[0].url) == "https://dashboard.pawabase.com/v1/projects"
    with pytest.raises(CloudError) as refused:
        cloud.projects.create("x")
    assert refused.value.status_code == 403 and refused.value.code == "insufficient_scope" and refused.value.details == {"needed": "projects:write"}


def test_create_then_wait_until_running_and_failures():
    polls = {"n": 0}

    def answer(request, i):
        if request.method == "POST":
            return httpx.Response(201, json={"ref": "p1", "state": "creating"})
        polls["n"] += 1
        return httpx.Response(200, json={"ref": "p1", "state": "creating" if polls["n"] < 3 else "running", "default_environment": "acme-p1-development"})

    transport, calls = serve(answer)
    cloud = Cloud(token="t", transport=transport)
    made = cloud.projects.create("Portal", app="bookings", values={"SITE_URL": "https://x.example"})
    assert json.loads(calls[0].content) == {"name": "Portal", "app": "bookings", "values": {"SITE_URL": "https://x.example"}}
    ready = cloud.projects.wait_until_running(made["ref"], interval=0.001)
    assert ready["default_environment"] == "acme-p1-development" and polls["n"] == 3
    failed, _ = serve(lambda r, i: httpx.Response(200, json={"ref": "p1", "state": "failed"}))
    with pytest.raises(RuntimeError, match="ended up failed"):
        Cloud(token="t", transport=failed).projects.wait_until_running("p1", interval=0.001)
    broken, _ = serve(lambda r, i: httpx.Response(200, json={"ref": "p1", "state": "running", "app": {"state": "failed", "error": "not empty"}}))
    with pytest.raises(RuntimeError, match="not empty"):
        Cloud(token="t", transport=broken).projects.wait_for_app("p1", interval=0.001)


def test_create_key_maps_options_and_the_studio_api_reaches_a_target():
    transport, calls = serve(lambda r, i: httpx.Response(201, json={"id": "k1", "key": "pb_sk_abc", "data": []}))
    cloud = Cloud(token="t", transport=transport)
    made = cloud.projects.create_key("p1", "Lumen", role="secret", allowed_ips=["10.0.0.0/8"], expires_at="2030-01-01T00:00:00Z", environment="acme-p1-staging")
    assert made["key"] == "pb_sk_abc"
    assert str(calls[0].url) == "https://dashboard.pawabase.com/v1/projects/p1/api-keys?environment=acme-p1-staging"
    assert json.loads(calls[0].content) == {"name": "Lumen", "role": "secret", "allowed_ips": ["10.0.0.0/8"], "expires_at": "2030-01-01T00:00:00Z"}
    cloud.studio("p1").get("platform", "envs/acme-p1-development/resources", {"q": "orders"})
    cloud.studio("p1").post("platform", "/envs/acme-p1-development/resources", {"name": "orders"})
    assert str(calls[1].url) == "https://dashboard.pawabase.com/v1/projects/p1/studio/platform/envs/acme-p1-development/resources?q=orders"
    assert calls[2].method == "POST"


def test_the_async_classes_are_the_same_awaited():
    async def run():
        polls = {"n": 0}

        def answer(request, i):
            if "oauth" in request.url.path:
                return httpx.Response(200, json={**REPLY, "access_token": "at2", "refresh_token": "pbr_2"})
            polls["n"] += 1
            return httpx.Response(200, json={"ref": "p1", "state": "creating" if polls["n"] < 2 else "running"})

        transport, calls = serve(answer)
        oauth = AsyncOAuth(**APP, transport=transport)
        saved = []

        async def on_refresh(t):
            saved.append(t.refresh_token)

        source = oauth.token_source(OAuthTokens("at1", "pbr_1", time.time() - 10, [], "acme"), on_refresh=on_refresh)
        async with AsyncCloud(token=source, transport=transport) as cloud:
            project = await cloud.projects.wait_until_running("p1", interval=0.001)
        assert project["state"] == "running" and saved == ["pbr_2"]
        assert calls[1].headers["authorization"] == "Bearer at2"

    asyncio.run(run())
