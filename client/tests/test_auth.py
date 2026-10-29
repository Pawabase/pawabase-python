import time

import httpx
import pytest

from pawabase import AuthenticationError, ConfigError, FileSessionStore, QuotaExceededError
from tests.conftest import make, make_async

USER = {"id": "u1", "email": "ada@x.io", "identities": [{"id": 1, "provider": "google"}]}


def session(access="a1", refresh="r1", expires_in=900):
    return {"access_token": access, "refresh_token": refresh, "expires_in": expires_in, "user": USER, "session_id": "s1", "org": None, "org_role": None}


def test_sign_in_keeps_the_session_and_sends_its_token(wire, pb):
    wire.answer(httpx.Response(200, json=session()))
    result = pb.auth.sign_in_with_password(email="ada@x.io", password="pw")
    assert result.status == "signed_in" and pb.auth.access_token == "a1" and pb.auth.user["email"] == "ada@x.io"
    assert wire.json() == {"grant_type": "password", "email": "ada@x.io", "password": "pw"}
    assert "authorization" not in wire.last.headers  # signing in sends no stale token
    wire.answer(httpx.Response(200, json=USER))
    pb.auth.get_user()
    assert wire.last.headers["authorization"] == "Bearer a1" and wire.last.headers["apikey"] == "pb_pk_test"


def test_mfa_sign_in_is_finished_with_a_code(wire, pb):
    wire.answer(httpx.Response(200, json={"mfa_required": True, "mfa_token": "m1", "factors": ["totp"]}))
    result = pb.auth.sign_in_with_password(email="ada@x.io", password="pw")
    assert result.status == "mfa_required" and result.factors == ["totp"] and pb.auth.session is None
    wire.answer(httpx.Response(200, json=session()))
    assert result.verify("123456").access_token == "a1"
    assert wire.json() == {"grant_type": "mfa", "mfa_token": "m1", "code": "123456"}


def test_sign_up_may_wait_for_email_verification(wire, pb):
    wire.answer(httpx.Response(201, json={"verification_required": True, "user": USER}))
    outcome = pb.auth.sign_up(email="ada@x.io", password="pw", name="Ada", data={"plan": "free"})
    assert outcome.verification_required and outcome.session is None and pb.auth.session is None
    assert wire.json() == {"email": "ada@x.io", "password": "pw", "name": "Ada", "data": {"plan": "free"}}


def test_an_expiring_token_is_renewed_before_the_request(wire, pb):
    wire.answer(httpx.Response(200, json=session(expires_in=5)))
    pb.auth.sign_in_with_password(email="ada@x.io", password="pw")
    wire.answer(lambda request: httpx.Response(200, json=session("a2", "r2")) if request.url.path == "/auth/v1/token" else httpx.Response(200, json=USER))
    pb.auth.get_user()
    paths = [r.url.path for r in wire.requests]
    assert paths[-2:] == ["/auth/v1/token", "/auth/v1/user"]
    assert wire.json(-2)["grant_type"] == "refresh_token" and wire.last.headers["authorization"] == "Bearer a2"


def test_a_rejected_token_is_renewed_once_and_the_request_repeated(wire, pb):
    wire.answer(httpx.Response(200, json=session()))
    pb.auth.sign_in_with_password(email="ada@x.io", password="pw")
    seen = {"n": 0}

    def serve(request):
        if request.url.path == "/auth/v1/token":
            return httpx.Response(200, json=session("a2", "r2"))
        seen["n"] += 1
        return httpx.Response(401, json={"error": "expired"}) if seen["n"] == 1 else httpx.Response(200, json=USER)

    wire.answer(serve)
    assert pb.auth.get_user()["id"] == "u1" and pb.auth.access_token == "a2"


def test_a_dead_refresh_token_ends_the_session(wire, pb):
    wire.answer(httpx.Response(200, json=session(expires_in=5)))
    pb.auth.sign_in_with_password(email="ada@x.io", password="pw")
    wire.answer(httpx.Response(401, json={"error": "invalid_refresh_token"}))
    with pytest.raises(AuthenticationError):
        pb.auth.get_user()
    assert pb.auth.session is None


def test_sign_out_forgets_the_session_even_when_the_call_fails(wire, pb):
    wire.answer(httpx.Response(200, json=session()))
    pb.auth.sign_in_with_password(email="ada@x.io", password="pw")
    events = []
    pb.auth.on_auth_state_change(lambda event, s: events.append(event))
    wire.answer(httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(Exception):  # noqa: B017 - the failure is reported, the session is gone regardless
        pb.auth.sign_out()
    assert pb.auth.session is None and events == ["SIGNED_OUT"]


def test_a_session_survives_in_a_file_store(wire, tmp_path):
    store = FileSessionStore(tmp_path / "s.json")
    first = make(wire, session_store=store)
    wire.answer(httpx.Response(200, json=session()))
    first.auth.sign_in_with_password(email="ada@x.io", password="pw")
    assert oct((tmp_path / "s.json").stat().st_mode & 0o777) == "0o600"
    assert make(wire, session_store=store).auth.access_token == "a1"


def test_oauth_urls_never_carry_a_secret_key(wire):
    assert make(wire).auth.oauth_url("google", redirect_to="https://app.example/cb") == (
        "http://gateway.test/auth/v1/authorize/development/google?redirect_to=https%3A%2F%2Fapp.example%2Fcb&apikey=pb_pk_test"
    )
    with pytest.raises(ConfigError, match="secret key"):
        make(wire, key="pb_sk_x").auth.oauth_url("google")


def test_as_user_acts_for_one_user_without_touching_the_original(wire, pb):
    wire.answer(httpx.Response(200, json=[]))
    view = pb.as_user("user-token")
    view.from_("orders").execute()
    assert wire.last.headers["authorization"] == "Bearer user-token"
    pb.from_("orders").execute()
    assert "authorization" not in wire.last.headers


def test_the_account_surface(wire, pb):
    wire.answer(httpx.Response(200, json={"data": [{"id": "s1"}]}))
    assert pb.auth.sessions.list() == [{"id": "s1"}]
    pb.auth.mfa.status(); pb.auth.mfa.enroll(); pb.auth.devices.list(); pb.auth.api_keys.create(name="ci", scopes=["a"], expires_in_days=30)
    assert wire.json() == {"name": "ci", "scopes": ["a"], "expires_in_days": 30}
    pb.auth.orgs.invite("acme", email="b@x.io", role="admin")
    assert wire.last.url.path == "/auth/v1/orgs/acme/invitations" and wire.json() == {"email": "b@x.io", "role": "admin"}
    pb.auth.orgs.add_team_member("acme", "eng", "u2")
    assert wire.last.method == "PUT" and wire.last.url.path == "/auth/v1/orgs/acme/teams/eng/members/u2"


def test_quota_refusals_have_their_own_error(wire, pb):
    wire.answer(httpx.Response(403, json={"error": "quota_exceeded", "message": "full", "details": {"limit": "max_webhooks", "allowed": 3, "used": 3}}))
    with pytest.raises(QuotaExceededError) as raised:
        pb.from_("posts").create()
    assert raised.value.details["limit"] == "max_webhooks"


async def test_async_sign_in_and_automatic_renewal(wire):
    pb = make_async(wire)
    wire.answer(httpx.Response(200, json=session(expires_in=5)))
    assert (await pb.auth.sign_in_with_password(email="ada@x.io", password="pw")).status == "signed_in"
    wire.answer(lambda request: httpx.Response(200, json=session("a2", "r2")) if request.url.path == "/auth/v1/token" else httpx.Response(200, json=USER))
    assert (await pb.auth.get_user())["id"] == "u1" and pb.auth.access_token == "a2"
    assert time.time() < pb.auth.session.expires_at
