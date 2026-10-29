"""Helpers shared by the blocking and the async client."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

import httpx

from .errors import ConfigError

RETRIED_STATUSES = frozenset({502, 503, 504})
#: A 429 means the request was *not* processed, so it is safe to wait and send it again whatever the method.
MAX_RATE_WAITS = 8
MAX_RATE_WAIT_SECONDS = 20.0


def rate_wait(response: httpx.Response) -> float:
    """How long the gateway asked to wait (``Retry-After`` or the body's ``retry_after``), at least one second, at most 20."""
    asked: Any = response.headers.get("retry-after")
    if asked is None:
        try:
            asked = response.json().get("retry_after")
        except (ValueError, AttributeError):
            asked = None
    try:
        return min(max(float(asked), 1.0), MAX_RATE_WAIT_SECONDS)
    except (TypeError, ValueError):
        return 1.0


def then(value: Any, function: Callable[[Any], Any]) -> Any:
    """Apply *function* to a result that is a value (blocking client) or an awaitable (async client): the logic is written once."""
    if inspect.isawaitable(value):

        async def finish() -> Any:
            return function(await value)

        return finish()
    return function(value)


def chain(value: Any, function: Callable[[Any], Any]) -> Any:
    """Like :func:`then`, for a *function* that itself returns a value or an awaitable."""
    if inspect.isawaitable(value):

        async def finish() -> Any:
            out = function(await value)
            return await out if inspect.isawaitable(out) else out

        return finish()
    return function(value)


def path_of(*parts: Any) -> str:
    """Join URL path parts, quoting each (a key may contain ``/``: it stays a path)."""
    return "/".join(quote(str(p), safe="") for p in parts)


def key_path(key: str) -> str:
    """An object key as a path: segments quoted, slashes kept."""
    return "/".join(quote(segment, safe="") for segment in key.lstrip("/").split("/"))


def compact(values: Mapping[str, Any]) -> dict[str, Any]:
    """*values* without the ``None``\\ s."""
    return {k: v for k, v in values.items() if v is not None}


def is_secret_key(key: str) -> bool:
    return key.startswith("pb_sk_")


def require_async(client: Any, what: str) -> None:
    if not getattr(client, "is_async", False):
        raise ConfigError(f"{what} needs the async client: use AsyncPawabase.")
