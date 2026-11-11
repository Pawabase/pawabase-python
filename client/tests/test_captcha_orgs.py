import hashlib

import httpx
import pytest

from pawabase import PawabaseError
from pawabase.captcha import leading_zero_bits, solve
from tests.conftest import make, make_async

PUZZLE = {"provider": "pawabase", "action": "contact", "challenge": "Ym9keQ.sig", "salt": "s4ltS4ltS4ltS4lt", "bits": 10, "expires_at": 9e9}


def test_solve_finds_an_answer_the_server_checks():
    token = solve(PUZZLE)
    prefix, body, signature, nonce = token.split(".")
    assert (prefix, f"{body}.{signature}") == ("pow1", PUZZLE["challenge"])
    assert leading_zero_bits(hashlib.sha256(f"{PUZZLE['salt']}:{nonce}".encode()).digest()) >= 10
    with pytest.raises(ValueError):
        solve({"provider": "turnstile"})


def flow(request: httpx.Request, refused: list[int]) -> httpx.Response:
    import json

    body = json.loads(request.content or b"{}")
    if request.url.path.endswith("/challenge"):
        return httpx.Response(200, json={"required": True, "mode": "always", "captcha": PUZZLE})
    if request.url.path.endswith("/redeem"):
        if refused and refused.pop():
            return httpx.Response(403, json={"error": "captcha_failed", "message": "no", "details": {"captcha": {**PUZZLE, "salt": "freshFreshFresh1"}}})
        assert body["token"].startswith("pow1.")
        return httpx.Response(200, json={"pass": "pbc1.ok", "action": "contact", "expires_at": 9e9})
    if request.url.path.endswith("/siteverify"):
        return httpx.Response(200, json={"success": body["response"] == "pbc1.ok", "action": "contact", "error-codes": []})
    raise AssertionError(request.url.path)


def test_protect_earns_a_pass_and_verify_checks_it(wire):
    refused = [1]  # the first answer is refused with a fresh puzzle: it is solved again
    wire.answer(lambda request: flow(request, refused))
    pb = make(wire)
    assert pb.captcha.protect("contact")["pass"] == "pbc1.ok"
    assert [r.url.path for r in wire.requests] == ["/auth/v1/captcha/challenge", "/auth/v1/captcha/redeem", "/auth/v1/captcha/redeem"]
    server = make(wire, key="pb_sk_test")
    assert server.captcha.verify("pbc1.ok", action="contact", remoteip="203.0.113.5")["success"] is True
    assert wire.json() == {"response": "pbc1.ok", "action": "contact", "remoteip": "203.0.113.5"}


async def test_the_async_client_does_the_same(wire):
    refused = [1]
    wire.answer(lambda request: flow(request, refused))
    pb = make_async(wire)
    assert (await pb.captcha.protect("contact"))["pass"] == "pbc1.ok"
    assert len([r for r in wire.requests if r.url.path.endswith("/redeem")]) == 2


def test_a_hosted_provider_needs_get_token(wire):
    hosted = {"provider": "turnstile", "site_key": "0x4", "action": "contact"}
    wire.answer(lambda request: httpx.Response(200, json={"required": True, "mode": "always", "captcha": hosted}) if request.url.path.endswith("/challenge") else httpx.Response(200, json={"pass": "pbc1.w"}))
    pb = make(wire)
    with pytest.raises(PawabaseError) as missing:
        pb.captcha.protect("contact")
    assert missing.value.code == "captcha_widget_needed"
    assert pb.captcha.protect("contact", get_token=lambda c: "widget-token")["pass"] == "pbc1.w"
    assert wire.json() == {"action": "contact", "token": "widget-token"}


def test_auth_calls_carry_a_pass(wire, pb):
    wire.answer(httpx.Response(200, json={"sent": True}))
    pb.auth.request_password_reset(email="ada@x.io", captcha_token="pbc1.p")
    assert wire.json() == {"email": "ada@x.io", "captcha_token": "pbc1.p"}
    pb.auth.send_email_code(email="ada@x.io", captcha_token="pbc1.q")
    assert wire.json()["captcha_token"] == "pbc1.q"
    wire.answer(httpx.Response(201, json={"access_token": "a", "refresh_token": "r", "expires_in": 900, "user": {"id": "u"}}))
    pb.auth.sign_in_anonymously(captcha_token="pbc1.r")
    assert wire.last.headers["x-captcha-token"] == "pbc1.r"


def test_search_sends_q(wire, pb):
    wire.answer(httpx.Response(200, json={"data": [], "page": 1, "per_page": 20, "total": 0}))
    pb.from_("posts").search("  pawabase  ").execute()
    assert wire.query()["q"] == "pawabase"
    pb.from_("posts").search("").execute()
    assert "q" not in wire.query()


def test_organizations_have_every_management_call(wire, pb):
    wire.answer(lambda request: httpx.Response(200, json={"data": [], "next": None}))
    orgs = pb.auth.orgs
    orgs.roles("acme")
    orgs.permissions("acme")
    orgs.save_role("acme", "support", name="Support", permissions=["tickets.read"])
    orgs.delete_role("acme", "support")
    orgs.transfer("acme", user_id="u2", keep_role="owner")
    orgs.update_team("acme", "eng", role=None)
    orgs.team_members("acme", "eng")
    orgs.delete_team("acme", "eng")
    orgs.add_domain("acme", domain="acme.test", join="suggest")
    orgs.verify_domain("acme", "acme.test")
    orgs.update_domain("acme", "acme.test", join="join")
    orgs.remove_domain("acme", "acme.test")
    orgs.set_security("acme", require_mfa=True)
    orgs.audit("acme", limit=20, before=99)
    orgs.joinable()
    orgs.join("acme")
    assert [f"{r.method} {r.url.raw_path.decode()}" for r in wire.requests] == [
        "GET /auth/v1/orgs/acme/roles",
        "GET /auth/v1/orgs/acme/permissions",
        "PUT /auth/v1/orgs/acme/roles/support",
        "DELETE /auth/v1/orgs/acme/roles/support",
        "POST /auth/v1/orgs/acme/transfer",
        "PATCH /auth/v1/orgs/acme/teams/eng",
        "GET /auth/v1/orgs/acme/teams/eng/members",
        "DELETE /auth/v1/orgs/acme/teams/eng",
        "POST /auth/v1/orgs/acme/domains",
        "POST /auth/v1/orgs/acme/domains/acme.test/verify",
        "PATCH /auth/v1/orgs/acme/domains/acme.test",
        "DELETE /auth/v1/orgs/acme/domains/acme.test",
        "PATCH /auth/v1/orgs/acme/security",
        "GET /auth/v1/orgs/acme/audit?limit=20&before=99",
        "GET /auth/v1/joinable-orgs",
        "POST /auth/v1/joinable-orgs/acme/join",
    ]
    assert wire.json(5) == {"role": None}  # "no role" is sent, not dropped
    assert wire.json(4) == {"user_id": "u2", "keep_role": "owner"}
