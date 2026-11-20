"""``pb.bachs``: take payments with the Bachs integration, without calling REST by hand.

Install the integration in Studio (Integrations → Bachs), then::

    started = pb.bachs.payments.create(amount="5000.00", currency="NGN", success_url="https://shop.example.com/thanks")
    redirect(started["checkout_url"])                       # the buyer pays on Bachs
    payment = pb.bachs.payments.wait_for(started["reference"])   # paid, expired, flagged, …

On ``AsyncPawabase`` every method is awaited. A payment is only ever marked paid by Pawabase, after it asked Bachs: this client starts
payments and reads what Pawabase recorded. Every ``create`` carries an Idempotency-Key, so a retry after a dropped connection gives back
the same payment instead of starting a second one.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Sequence
from typing import Any

from . import _core
from .errors import PawabaseError

PENDING = "pending"
STATUSES = ("pending", "paid", "flagged", "expired", "cancelled", "failed")


class Payments:
    def __init__(self, client: Any) -> None:
        self._c = client

    def create(
        self,
        *,
        amount: str | int | float,
        currency: str,
        success_url: str,
        cancel_url: str | None = None,
        email: str | None = None,
        name: str | None = None,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Start a payment and get the checkout link: ``{"payment_id", "reference", "status", "checkout_url", "expires_at", "replayed"}``.

        ``idempotency_key`` stops a retry starting a second payment (the same key asks for the same payment). It is made for you when
        left out, and kept for the client's own retries; pass your own (an order id works well) to make a retry from your server safe too.
        """
        body = {
            "amount": str(amount),
            "currency": currency,
            "success_url": success_url,
            **{k: v for k, v in {"cancel_url": cancel_url, "email": email, "name": name, "description": description, "metadata": metadata}.items() if v is not None},
        }

        def finish(response: Any) -> dict[str, Any]:
            out = dict(response.json())
            out["replayed"] = response.headers.get("idempotent-replayed") == "true"
            return out

        return _core.then(
            self._c._call("POST", "/rest/v1/bachs/payments", json=body, headers={"Idempotency-Key": idempotency_key or str(uuid.uuid4())}, retry=True, raw=True),
            finish,
        )

    def get(self, reference: str) -> Any:
        """One of the signed-in buyer's own payments (a row of your ``bachs_payments`` table). A pending one is checked with Bachs first."""
        return _core.then(
            self._c._call("GET", f"/rest/v1/bachs/payments/{_core.key_path(reference)}", retry=True),
            lambda body: body["payment"],
        )

    def list(self, *, status: str | None = None, page: int | None = None, per_page: int | None = None) -> Any:
        """The signed-in buyer's payments, newest first, as a ``Page``."""
        return self._c.from_("bachs_payments").list(where={"status": status} if status else None, sort="-created_at", page=page, per_page=per_page)

    def wait_for(self, reference: str, *, until: Sequence[str] | None = None, timeout: float = 120.0, interval: float = 2.0) -> Any:
        """Wait until the payment is no longer pending (or reaches one of ``until``) and return it.

        Each check asks Pawabase, which asks Bachs, so it does not depend on the webhook having arrived. Raises ``TimeoutError`` if it is
        still pending at the deadline.
        """
        wanted = set(until) if until else None

        def done(payment: dict[str, Any]) -> bool:
            return payment["status"] in wanted if wanted else payment["status"] != PENDING

        deadline = time.monotonic() + timeout
        if getattr(self._c, "is_async", False):

            async def poll() -> dict[str, Any]:
                while True:
                    payment = await self.get(reference)
                    if done(payment):
                        return payment
                    if time.monotonic() + interval > deadline:
                        raise TimeoutError(f"payment {reference} was still pending after {timeout:g} seconds")
                    await asyncio.sleep(interval)

            return poll()
        while True:
            payment = self.get(reference)
            if done(payment):
                return payment
            if time.monotonic() + interval > deadline:
                raise TimeoutError(f"payment {reference} was still pending after {timeout:g} seconds")
            time.sleep(interval)


class Bachs:
    """``pb.bachs``."""

    def __init__(self, client: Any) -> None:
        self.payments = Payments(client)


__all__ = ["Bachs", "Payments", "PawabaseError", "STATUSES"]
