import json

import httpx
import pytest

from pawabase import PawabaseError
from tests.conftest import make, make_async

STARTED = {"payment_id": "01J", "reference": "bp_abc", "status": "pending", "checkout_url": "https://checkout.bachs.io/c1", "expires_at": "2099-01-01T00:00:00Z"}


def row(status):
    return {"payment": {"id": "01J", "reference": "bp_abc", "amount": "5000.00", "currency": "NGN", "status": status}}


def test_create_posts_with_an_idempotency_key(wire, pb):
    wire.answer(httpx.Response(201, json=STARTED))
    made = pb.bachs.payments.create(amount=5000, currency="NGN", success_url="https://shop.example.com/ok", metadata={"order_id": "7"})
    assert made["checkout_url"] == "https://checkout.bachs.io/c1" and made["replayed"] is False
    assert wire.last.method == "POST" and wire.last.url.path == "/rest/v1/bachs/payments"
    assert wire.json() == {"amount": "5000", "currency": "NGN", "success_url": "https://shop.example.com/ok", "metadata": {"order_id": "7"}}
    assert len(wire.last.headers["idempotency-key"]) > 8


def test_a_given_key_is_sent_and_a_replay_says_so(wire, pb):
    wire.answer(httpx.Response(200, json=STARTED, headers={"Idempotent-Replayed": "true"}))
    made = pb.bachs.payments.create(amount="1.00", currency="NGN", success_url="https://x.example/ok", idempotency_key="order-7")
    assert wire.last.headers["idempotency-key"] == "order-7" and made["replayed"] is True


def test_a_retry_after_a_gateway_error_reuses_the_key():
    from tests.conftest import Wire

    wire = Wire()
    pb = make(wire)
    pb.retries = 1
    calls = []

    def handler(request):
        calls.append(request.headers["idempotency-key"])
        return httpx.Response(503, json={"error": "unavailable"}) if len(calls) == 1 else httpx.Response(201, json=STARTED)

    wire.answer(handler)
    made = pb.bachs.payments.create(amount="9.00", currency="NGN", success_url="https://x.example/ok")
    assert made["reference"] == "bp_abc" and len(calls) == 2 and calls[0] == calls[1]


def test_a_refusal_raises(wire, pb):
    wire.answer(httpx.Response(422, json={"error": "currency_not_accepted", "message": "That currency is not accepted."}))
    with pytest.raises(PawabaseError) as err:
        pb.bachs.payments.create(amount="1", currency="XXX", success_url="https://x.example/ok")
    assert err.value.status_code == 422 and err.value.code == "currency_not_accepted"


def test_get_and_list(wire, pb):
    wire.answer(httpx.Response(200, json=row("paid")))
    assert pb.bachs.payments.get("bp_abc")["status"] == "paid"
    assert wire.last.url.path == "/rest/v1/bachs/payments/bp_abc"
    wire.answer(httpx.Response(200, json={"data": [{"reference": "bp_1", "status": "paid"}], "page": 1, "per_page": 20, "total": 1}))
    page = pb.bachs.payments.list(status="paid")
    assert page.data[0]["reference"] == "bp_1"
    assert wire.last.url.path == "/rest/v1/bachs_payments" and wire.query()["filter[status]"] == "eq.paid" and wire.query()["sort"] == "-created_at"


def test_wait_for_polls_until_not_pending(wire, pb):
    seen = []

    def handler(request):
        seen.append(1)
        return httpx.Response(200, json=row("pending" if len(seen) < 3 else "paid"))

    wire.answer(handler)
    assert pb.bachs.payments.wait_for("bp_abc", interval=0.01)["status"] == "paid" and len(seen) == 3


def test_wait_for_times_out_and_honours_until(wire, pb):
    wire.answer(httpx.Response(200, json=row("pending")))
    with pytest.raises(TimeoutError):
        pb.bachs.payments.wait_for("bp_abc", interval=0.01, timeout=0.03)
    wire.answer(httpx.Response(200, json=row("expired")))
    assert pb.bachs.payments.wait_for("bp_abc", until=["paid", "expired"])["status"] == "expired"


@pytest.mark.asyncio
async def test_the_async_client_does_the_same(wire, apb):
    wire.answer(httpx.Response(201, json=STARTED))
    made = await apb.bachs.payments.create(amount="5.00", currency="NGN", success_url="https://x.example/ok")
    assert made["reference"] == "bp_abc" and made["replayed"] is False
    seen = []

    def handler(request):
        seen.append(1)
        return httpx.Response(200, json=row("pending" if len(seen) < 2 else "paid"))

    wire.answer(handler)
    assert (await apb.bachs.payments.wait_for("bp_abc", interval=0.01))["status"] == "paid"
    assert (await apb.bachs.payments.get("bp_abc"))["status"] == "paid"
