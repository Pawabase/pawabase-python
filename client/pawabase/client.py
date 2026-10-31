"""The Pawabase client: ``Pawabase`` (blocking) and ``AsyncPawabase`` share every method.

Authentication is the environment's key in the ``apikey`` header. A *publishable* key (``pb_pk_…``) is what a browser or an app holds: it
reaches the data plane under the environment's policies, and acts as a user once one signs in. A *secret* key (``pb_sk_…``) is what a server
holds: it bypasses policies, administers users (``pb.admin``) and manages the environment (``pb.manage``). Keep it off devices.

    pb = Pawabase("https://api.example.com", "pb_pk_…")
    pb.auth.sign_in_with_password(email="ada@example.com", password="…")
    posts = pb.from_("posts").eq("status", "live").order("created_at", "desc").limit(10).execute()
    pb.storage.from_("avatars").upload("ada.png", data, content_type="image/png")
    pb.flows.invoke("welcome", email="ada@example.com")

Everything is written once against ``_call``, which a blocking client answers with a value and an async client with an awaitable.
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Mapping
from typing import Any

import httpx

from . import _core
from .admin import Admin
from .auth import Auth, SessionStore
from .callables import Flows, Routes
from .data import AsyncResource, Resource
from .errors import NetworkError, PawabaseError, failure
from .manage import Manage
from .realtime import Realtime
from .storage import Storage

__all__ = ["AsyncPawabase", "Pawabase", "PawabaseError"]

USER_AGENT = "pawabase-python/0.3"


class _Base:
    resource_class: type = Resource
    is_async = False

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        environment: str = "development",
        timeout: float = 30.0,
        retries: int = 2,
        session_store: SessionStore | None = None,
        access_token: str | None = None,
        auto_refresh: bool = True,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.environment = environment
        self.timeout = timeout
        self.retries = retries
        self.auto_refresh = auto_refresh
        self.extra_headers = dict(headers or {})
        self._extra: dict[str, str] = {}
        self._borrowed = False
        self._store = session_store
        self._token = access_token
        self._bind()

    def _bind(self) -> None:
        """Create the modules over this client (again, for a view made by :meth:`as_user`)."""
        self.auth = Auth(self, self._store)
        self.storage = Storage(self)
        self.flows = Flows(self)
        self.routes = Routes(self)
        self.realtime = Realtime(self)
        self.manage = Manage(self)
        self.admin = Admin(self)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(url={self.url!r}, environment={self.environment!r}, api_key=<hidden>)"

    # ── data ─────────────────────────────────────────────────────────────

    def from_(self, resource: str) -> Any:
        """A resource's records: ``pb.from_("posts").eq("status", "live").execute()``, ``.get(id)``, ``.create(title="Hello")``."""
        return self.resource_class(self, resource)

    table = from_
    resource = from_

    # ── the connection ───────────────────────────────────────────────────

    @property
    def headers(self) -> dict[str, str]:
        # The gateway turns this key into a signed environment context; the raw key is never forwarded to a function or an internal service.
        return {"apikey": self.api_key, "x-environment": self.environment, "user-agent": USER_AGENT, **self.extra_headers}

    def as_user(self, token: str | None, **headers: str) -> Any:
        """A view of this client that acts as one signed-in user: the same connection pool, ``Authorization: Bearer <token>`` on every request.

        A web server holds one client for the process and takes one of these per request, so users never share credentials. Closing a
        view closes nothing.
        """
        view = copy.copy(self)
        view._extra = {**({"Authorization": f"Bearer {token}"} if token else {}), **{k.replace("_", "-"): v for k, v in headers.items()}}
        view._borrowed = True
        view._store = None
        view._token = None
        view._bind()
        view.auto_refresh = False
        return view

    def as_machine(self, *, client_id: str, client_secret: str, scope: str | None = None, **headers: str) -> Any:
        """A view of this client that acts as a machine (a service or an AI agent): it signs in with the machine's id and key, then behaves
        like :meth:`as_user` with the token it got. The token lives ``expires_in`` seconds (15 minutes by default); take a new view when it ends.

            agent = pb.as_machine(client_id=os.environ["PB_MACHINE_ID"], client_secret=os.environ["PB_MACHINE_KEY"])
            agent.from_("orders")...
        """
        return _core.then(
            self.auth.machine.token(client_id=client_id, client_secret=client_secret, scope=scope),
            lambda answer: self.as_user(answer["access_token"], **headers),
        )

    def url_for(self, path: str, **query: Any) -> str:
        """An absolute URL on the gateway (for links you hand out)."""
        return self.url + path + (("?" + str(httpx.QueryParams(_core.compact(query)))) if _core.compact(query) else "")

    def request(self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None) -> Any:
        """Any request, as the key's (and the signed-in user's) holder. ``path`` is relative to the gateway (``/rest/v1/posts``)."""
        return self._call(method, path if path.startswith("/") else "/" + path, json=json, params=params, retry=method.upper() in ("GET", "HEAD"))

    def status(self) -> Any:
        """``GET /health/v1``: whether the gateway and its services answer."""
        return self._call("GET", "/health/v1", retry=True, auth=False)

    # ── transport pieces shared by both clients ──────────────────────────

    def _headers_for(self, auth: bool, extra: Mapping[str, str] | None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if auth:
            token = self.auth.access_token or self._token
            if token and "Authorization" not in self._extra:
                headers["Authorization"] = f"Bearer {token}"
        headers.update(self._extra)
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _answer(response: httpx.Response, raw: bool | str) -> Any:
        if raw == "any":  # the response as it is, error statuses included (raw responses)
            return response
        if response.is_error:
            raise _failure(response)
        if raw:
            return response
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def _attempts(self, retry: bool) -> int:
        return 1 + (self.retries if retry else 0)


def _failure(response: httpx.Response) -> PawabaseError:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    return failure(response.status_code, detail, request_id=response.headers.get("x-request-id"))


class Pawabase(_Base):
    """Blocking client.

    Args:
        url: The gateway URL.
        api_key: A publishable or secret key of the environment.
        environment: The environment name, such as ``development`` (used by ``pb.manage`` and ``pb.admin``).
        timeout: Seconds per request.
        retries: Extra attempts for reads that fail with a connection error or a 502/503/504.
        session_store: Where a signed-in session is kept between runs (see :class:`~pawabase.auth.FileSessionStore`); memory by default.
        access_token: A user's access token to act as, without signing in here.
        auto_refresh: Renew an expiring or rejected access token with the refresh token.
        headers: Extra headers on every request.
    """

    def __init__(self, url: str, api_key: str, **options: Any) -> None:
        super().__init__(url, api_key, **options)
        self._client = httpx.Client(base_url=self.url, headers={k: v for k, v in self.headers.items()}, timeout=self.timeout)

    def close(self) -> None:
        if not self._borrowed:
            self._client.close()

    def __enter__(self) -> Pawabase:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _call(
        self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None, retry: bool = False, headers: Mapping[str, str] | None = None,
        content: Any = None, auth: bool = True, raw: bool = False, timeout: float | None = None,
    ) -> Any:
        if auth and self.auth.expiring():
            self.auth.refresh_session()
        last: Exception | None = None
        waits = refreshed = attempt = 0
        while attempt < self._attempts(retry):
            try:
                response = self._client.request(
                    method, path, json=json, params=params, content=content, headers=self._headers_for(auth, headers), timeout=timeout or self.timeout
                )
            except httpx.TransportError as error:
                last = NetworkError(0, f"Could not reach {self.url}: {error}")
            else:
                if response.status_code == 429 and waits < _core.MAX_RATE_WAITS:
                    waits += 1
                    time.sleep(_core.rate_wait(response))
                    continue
                if response.status_code == 401 and auth and not refreshed and self.auto_refresh and self.auth.can_refresh():
                    refreshed = 1
                    self.auth.refresh_session()
                    continue
                if response.status_code in _core.RETRIED_STATUSES and retry and attempt + 1 < self._attempts(retry):
                    last = _failure(response)
                else:
                    return self._answer(response, raw)
            time.sleep(min(0.25 * 2**attempt, 2.0))
            attempt += 1
        assert last is not None
        raise last

    def stream(self, method: str, path: str, **kwargs: Any) -> Any:
        """An open streaming response (``with pb.stream("GET", url) as r: for chunk in r.iter_bytes()``)."""
        return self._client.stream(method, path, headers=self._headers_for(True, kwargs.pop("headers", None)), **kwargs)


class AsyncPawabase(_Base):
    """Async client: the same methods, each returning an awaitable; ``pb.realtime`` is available here."""

    resource_class = AsyncResource
    is_async = True

    def __init__(self, url: str, api_key: str, **options: Any) -> None:
        super().__init__(url, api_key, **options)
        self._client = httpx.AsyncClient(base_url=self.url, headers={k: v for k, v in self.headers.items()}, timeout=self.timeout)
        self._refresh_lock: asyncio.Lock | None = None

    async def close(self) -> None:
        if not self._borrowed:
            await self._client.aclose()
        await self.realtime.close()

    async def __aenter__(self) -> AsyncPawabase:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def _call(  # type: ignore[override]
        self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None, retry: bool = False, headers: Mapping[str, str] | None = None,
        content: Any = None, auth: bool = True, raw: bool = False, timeout: float | None = None,
    ) -> Any:
        if auth and self.auth.expiring():
            await self._refresh_once()
        last: Exception | None = None
        waits = refreshed = attempt = 0
        while attempt < self._attempts(retry):
            try:
                response = await self._client.request(
                    method, path, json=json, params=params, content=content, headers=self._headers_for(auth, headers), timeout=timeout or self.timeout
                )
            except httpx.TransportError as error:
                last = NetworkError(0, f"Could not reach {self.url}: {error}")
            else:
                if response.status_code == 429 and waits < _core.MAX_RATE_WAITS:
                    waits += 1
                    await asyncio.sleep(_core.rate_wait(response))
                    continue
                if response.status_code == 401 and auth and not refreshed and self.auto_refresh and self.auth.can_refresh():
                    refreshed = 1
                    await self._refresh_once()
                    continue
                if response.status_code in _core.RETRIED_STATUSES and retry and attempt + 1 < self._attempts(retry):
                    last = _failure(response)
                else:
                    return self._answer(response, raw)
            await asyncio.sleep(min(0.25 * 2**attempt, 2.0))
            attempt += 1
        assert last is not None
        raise last

    async def _refresh_once(self) -> None:
        """One refresh at a time: concurrent requests that all found the token expired wait for the same renewal."""
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        stale = self.auth.access_token
        async with self._refresh_lock:
            if self.auth.access_token == stale:
                await self.auth.refresh_session()

    def stream(self, method: str, path: str, **kwargs: Any) -> Any:
        """An open streaming response (``async with pb.stream("GET", url) as r: async for chunk in r.aiter_bytes()``)."""
        return self._client.stream(method, path, headers=self._headers_for(True, kwargs.pop("headers", None)), **kwargs)
