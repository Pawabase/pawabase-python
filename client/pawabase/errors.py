"""Errors raised by the client.

Every failed request raises a :class:`PawabaseError`; the ones callers most often handle have their own subclass, so ``except NotFoundError``
reads better than ``except PawabaseError as e: if e.status_code == 404``. All of them keep the status, the body and the machine-readable code.
"""

from __future__ import annotations

from typing import Any


def message_of(detail: Any) -> str:
    if isinstance(detail, dict):
        inner = detail.get("detail", detail.get("message", detail.get("error")))
        if isinstance(inner, dict):
            return str(inner.get("message") or inner)
        if isinstance(inner, list):
            return "; ".join(str(i.get("msg", i)) if isinstance(i, dict) else str(i) for i in inner)
        return str(inner) if inner is not None else str(detail)
    return str(detail)


class PawabaseError(RuntimeError):
    """A Pawabase API request failed.

    Attributes:
        status_code: The HTTP status (``0`` when the gateway could not be reached at all).
        detail: The body the API answered with.
        code, message: The machine-readable code (``unique_violation``, ``quota_exceeded``…) and the sentence, when the body had them.
        request_id: The gateway's request id, for support, when it sent one.
    """

    def __init__(self, status_code: int, detail: Any, *, request_id: str | None = None) -> None:
        self.status_code, self.detail, self.request_id = status_code, detail, request_id
        self.code = detail.get("error") if isinstance(detail, dict) and isinstance(detail.get("error"), str) else None
        self.message = message_of(detail)
        super().__init__(f"Pawabase API returned {status_code}: {self.message}")

    @property
    def details(self) -> Any:
        """The structured ``details`` the API attached (the limit that was hit, the columns of a conflict), if any."""
        return self.detail.get("details") if isinstance(self.detail, dict) else None

    @property
    def problems(self) -> list[str]:
        """The per-item problems a refused deployment lists (``problems`` in the body), if any."""
        body = self.detail if isinstance(self.detail, dict) else {}
        for holder in (body, body.get("detail") if isinstance(body.get("detail"), dict) else {}):
            if isinstance(holder.get("problems"), list):
                return [str(p) for p in holder["problems"]]
        return []


class NetworkError(PawabaseError):
    """The gateway could not be reached."""


class AuthenticationError(PawabaseError):
    """401: no key, a wrong key, or a missing or expired user token."""


class ForbiddenError(PawabaseError):
    """403: the caller is known but may not do this (a policy, a firewall rule, a plan limit: see ``code``)."""


class NotFoundError(PawabaseError):
    """404."""


class ConflictError(PawabaseError):
    """409: for example a unique value that already exists (``details["columns"]``)."""


class ValidationError(PawabaseError):
    """400 or 422: the request was refused as invalid."""


class RateLimitedError(PawabaseError):
    """429 that outlasted the client's automatic waiting."""

    @property
    def retry_after(self) -> float | None:
        body = self.detail if isinstance(self.detail, dict) else {}
        try:
            return float(body.get("retry_after"))
        except (TypeError, ValueError):
            return None


class ServerError(PawabaseError):
    """5xx."""


class QuotaExceededError(ForbiddenError):
    """403 ``quota_exceeded``: the project's plan does not allow this (``details``: ``limit``, ``allowed``, ``used``)."""


class RealtimeError(PawabaseError):
    """A realtime request was refused or timed out. ``code`` says why (``forbidden``, ``ack_timeout``…)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(0, {"error": code, "message": message})


class ConfigError(RuntimeError):
    """The client was used in a way that cannot work (a secret key in a URL, realtime on a blocking client…)."""


def failure(status: int, detail: Any, *, request_id: str | None = None) -> PawabaseError:
    """The most specific error for an answer."""
    code = detail.get("error") if isinstance(detail, dict) else None
    kind: type[PawabaseError]
    if status == 403 and code == "quota_exceeded":
        kind = QuotaExceededError
    elif status == 401:
        kind = AuthenticationError
    elif status == 403:
        kind = ForbiddenError
    elif status == 404:
        kind = NotFoundError
    elif status == 409:
        kind = ConflictError
    elif status in (400, 422):
        kind = ValidationError
    elif status == 429:
        kind = RateLimitedError
    elif status >= 500:
        kind = ServerError
    else:
        kind = PawabaseError
    return kind(status, detail, request_id=request_id)
