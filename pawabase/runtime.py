"""``ctx.runtime`` for code that runs on your machine but works on a Pawabase deployment.

:class:`RemoteRuntime` has the same methods as the runtime a deployed function receives. Most of them are one HTTP call to the deployment
(``POST …/runtime/call``), executed there with the deployment's own database, cache, queues, storage and secrets, as the caller you name. So a function under
``pawabase emulate`` that reads an order, reserves stock, emits ``order.paid`` and dispatches a flow does exactly that, to the real environment.

Two things are deliberately **local**:

* ``http_request`` is made from your machine (a function that calls Paystack calls it from your laptop, so you can point it at a test double);
* ``log`` goes to the emulator's console (and into the invocation's log, which the emulator returns).

Remote SQL (``db()``, ``transaction()``) works the same way over ``…/runtime/db``. A transaction there is held open by the deployment for you; it is rolled
back after a minute of silence, and it lives in one server process, so it is a development tool, not something to put load through.

When the emulator is running locally-defined functions, ``call_function`` / ``dispatch_function`` prefer *your local copy* (set ``local_functions``): calling
``stock.reserve`` from another function runs the code you are editing, not the version that is deployed.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from . import codec
from .client import AsyncPawabase, PawabaseError
from .functions import FunctionError

logger = logging.getLogger("pawabase.runtime")


class RemoteError(FunctionError):
    """A runtime call failed on the deployment. ``status``/``code``/``message`` are what it answered with."""


class RemoteDb:
    """``db()``: SQL on the deployment's database. Same methods as the platform's session; values keep their types (datetimes stay datetimes)."""

    def __init__(self, runtime: RemoteRuntime, tx: str | None = None) -> None:
        self._runtime, self._tx = runtime, tx

    async def _op(self, op: str, **fields: Any) -> Any:
        return await self._runtime._db(op, tx=self._tx, **fields)

    async def fetch(self, statement: str, params: list[Any] | tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return await self._op("fetch", sql=statement, params=list(params))

    async def one(self, statement: str, params: list[Any] | tuple[Any, ...] = ()) -> dict[str, Any] | None:
        return await self._op("one", sql=statement, params=list(params))

    async def scalar(self, statement: str, params: list[Any] | tuple[Any, ...] = (), default: Any = None) -> Any:
        return await self._op("scalar", sql=statement, params=list(params), default=default)

    async def execute(self, statement: str, params: list[Any] | tuple[Any, ...] = ()) -> int:
        return await self._op("execute", sql=statement, params=list(params))

    async def insert(self, table: str, data: Mapping[str, Any]) -> dict[str, Any]:
        return await self._op("insert", table=table, data=dict(data))

    async def update(self, table: str, record_id: Any, data: Mapping[str, Any]) -> int:
        return await self._op("update", table=table, id=record_id, data=dict(data))

    async def delete(self, table: str, record_id: Any) -> int:
        return await self._op("delete", table=table, id=record_id)

    @staticmethod
    def now() -> dt.datetime:
        return dt.datetime.now(dt.UTC)


class RemoteTransaction:
    """``async with runtime.transaction() as db``: commit on success, roll back on any exception."""

    def __init__(self, runtime: RemoteRuntime) -> None:
        self._runtime, self._tx = runtime, None

    async def __aenter__(self) -> RemoteDb:
        self._tx = (await self._runtime._db("begin"))
        return RemoteDb(self._runtime, self._tx)

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        await self._runtime._db("rollback" if exc_type else "commit", tx=self._tx)
        return False


class RemoteRuntime:
    """The platform's capabilities, for a function running somewhere else.

    Args:
        client: A client for the deployment, holding a secret key with the ``runtime:use`` scope.
        auth: The caller the work is done as (``user_id``, ``roles``, ``permissions`` …). The emulator passes the real caller's.
        branch: The branch whose functions ``call_function`` / ``dispatch_function`` resolve against.
        local_functions: ``name -> async callable(input)``; functions defined locally win over the deployed ones.
        http: The client for ``http_request``; yours by default.
    """

    def __init__(
        self,
        client: AsyncPawabase,
        *,
        auth: Mapping[str, Any] | None = None,
        branch: str | None = None,
        local_functions: Mapping[str, Callable[[Any], Any]] | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.client = client
        self.auth = dict(auth or {})
        self.branch = branch
        self.local_functions = local_functions or {}
        self._http = http or httpx.AsyncClient(timeout=30.0, follow_redirects=False)
        self._owns_http = http is None
        self.logs: list[dict[str, Any]] = []
        self._secrets: dict[str, str | None] = {}

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def for_caller(self, auth: Mapping[str, Any] | None, *, branch: str | None = None) -> RemoteRuntime:
        """A runtime for one invocation: the same connection, acting as *auth* (and with its own log)."""
        other = RemoteRuntime(self.client, auth=auth, branch=branch or self.branch, local_functions=self.local_functions, http=self._http)
        other._secrets = self._secrets
        return other

    # ── transport ───────────────────────────────────────────────────────

    async def _rpc(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            answer = await self.client.runtime_call(method, codec.encode(list(args)), codec.encode(kwargs), as_user=self.auth or None, branch=self.branch)
        except PawabaseError as error:
            raise _remote(error) from error
        return codec.decode(answer.get("result"))

    async def _db(self, op: str, **fields: Any) -> Any:
        body = {k: codec.encode(v) for k, v in fields.items() if v is not None}
        try:
            answer = await self.client.runtime_db(op, **body)
        except PawabaseError as error:
            raise _remote(error) from error
        if op == "begin":
            return answer["tx"]
        return codec.decode(answer.get("result"))

    # ── data ────────────────────────────────────────────────────────────

    async def resource_list(self, resource: str, *, filters: Mapping[str, Any] | None = None, sort: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        return await self._rpc("resource_list", resource, filters=dict(filters or {}), sort=sort, limit=limit, offset=offset)

    async def resource_get(self, resource: str, record_id: Any) -> dict[str, Any] | None:
        return await self._rpc("resource_get", resource, record_id)

    async def resource_create(self, resource: str, data: Mapping[str, Any]) -> dict[str, Any]:
        return await self._rpc("resource_create", resource, dict(data))

    async def resource_update(self, resource: str, record_id: Any, data: Mapping[str, Any]) -> dict[str, Any] | None:
        return await self._rpc("resource_update", resource, record_id, dict(data))

    async def resource_delete(self, resource: str, record_id: Any) -> bool:
        return await self._rpc("resource_delete", resource, record_id)

    async def db_query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        return await self._rpc("db_query", sql, list(params or []))

    async def db_transaction(self, operations: list[Mapping[str, Any]]) -> list[Any]:
        return await self._rpc("db_transaction", [dict(op) for op in operations])

    async def db(self) -> RemoteDb:
        return RemoteDb(self)

    def transaction(self) -> RemoteTransaction:
        return RemoteTransaction(self)

    # ── cache ───────────────────────────────────────────────────────────

    async def cache_get(self, key: str) -> Any:
        return await self._rpc("cache_get", key)

    async def cache_set(self, key: str, value: Any, ttl: int | None = None, tags: list[str] | None = None) -> None:
        await self._rpc("cache_set", key, value, ttl=ttl, tags=tags)

    async def cache_delete(self, key: str) -> bool:
        return await self._rpc("cache_delete", key)

    async def cache_invalidate(self, tags: list[str]) -> int:
        return await self._rpc("cache_invalidate", list(tags))

    # ── events, flows, jobs, realtime ───────────────────────────────────

    async def emit(self, name: str, payload: Any) -> str:
        """Publish an event on the deployment: its subscriptions and flows run there."""
        return await self._rpc("emit", name, payload)

    async def dispatch_flow(self, flow: str, input: Any, *, delay: int = 0, queue: str | None = None) -> str:
        return await self._rpc("dispatch_flow", flow, input, delay=delay, queue=queue)

    async def call_flow(self, flow: str, input: Any) -> Any:
        return await self._rpc("call_flow", flow, input)

    async def dispatch_function(self, function: str, input: Any, *, delay: int = 0, queue: str | None = None) -> str:
        """Queue a function as a job. Jobs run on the deployment, so this queues the *deployed* function (``call_function`` prefers your local one)."""
        return await self._rpc("dispatch_function", function, input, delay=delay, queue=queue)

    async def call_function(self, name: str, input: Any) -> Any:
        local = self.local_functions.get(name)
        if local is not None:
            return await local(input)
        return await self._rpc("call_function", name, input)

    async def publish(self, channel: str, event: str, payload: Any) -> dict[str, Any]:
        return await self._rpc("publish", channel, event, payload)

    # ── storage and mail ────────────────────────────────────────────────

    async def storage_put(self, bucket: str, key: str, content: bytes, content_type: str = "") -> dict[str, Any]:
        return await self._rpc("storage_put", bucket, key, content, content_type)

    async def storage_read(self, bucket: str, key: str, limit: int = 1_048_576) -> bytes:
        return await self._rpc("storage_read", bucket, key, limit)

    async def storage_signed_url(self, bucket: str, key: str, method: str = "GET", expires_in: int = 300) -> str:
        return await self._rpc("storage_signed_url", bucket, key, method, expires_in)

    async def storage_delete(self, bucket: str, key: str) -> bool:
        return await self._rpc("storage_delete", bucket, key)

    async def send_mail(self, to: list[str], subject: str, *, text: str | None = None, html: str | None = None, template: str | None = None, data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return await self._rpc("send_mail", list(to), subject, text=text, html=html, template=template, data=dict(data or {}))

    # ── outbound (local) ────────────────────────────────────────────────

    async def http_request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None, json: Any = None, params: Mapping[str, Any] | None = None, timeout: float = 30.0, retries: int = 0) -> dict[str, Any]:
        last: Exception | None = None
        for _ in range(max(1, retries + 1)):
            try:
                response = await self._http.request(method.upper(), url, headers=dict(headers or {}), json=json, params=params, timeout=timeout)
            except httpx.TransportError as error:
                last = error
                continue
            text = response.text
            body: Any = text
            if text and "json" in response.headers.get("content-type", ""):
                try:
                    body = response.json()
                except ValueError:
                    body = text
            if response.status_code in (408, 425, 429, 500, 502, 503, 504) and retries:
                last = None
                continue
            return {"status": response.status_code, "headers": dict(response.headers), "body": body}
        if last is not None:
            raise last
        return {"status": response.status_code, "headers": dict(response.headers), "body": body}

    async def webhook_send(self, event: str, payload: Any) -> int:
        return await self._rpc("webhook_send", event, payload)

    # ── platform ────────────────────────────────────────────────────────

    async def secret(self, name: str) -> str | None:
        """A secret's value. Needs the key's ``runtime:use`` scope; each read is audited on the deployment. Cached for the life of the emulator."""
        if name not in self._secrets:
            self._secrets[name] = await self._rpc("secret", name)
        return self._secrets[name]

    async def identity_user(self, user_id: str) -> dict[str, Any] | None:
        return await self._rpc("identity_user", user_id)

    async def check_policy(self, ref: Any, context: Mapping[str, Any]) -> bool:
        return await self._rpc("check_policy", ref, dict(context))

    async def log(self, level: str, message: str, data: Any = None) -> None:
        entry = {"level": level, "message": message, "data": data}
        self.logs.append(entry)
        logger.log({"debug": logging.DEBUG, "warning": logging.WARNING, "error": logging.ERROR}.get(str(level).lower(), logging.INFO), "%s %s", message, data if data is not None else "")

    async def metric(self, name: str, value: float = 1.0, tags: Mapping[str, str] | None = None) -> None:
        await self._rpc("metric", name, value, tags=dict(tags or {}))


def _remote(error: PawabaseError) -> RemoteError:
    detail = error.detail if isinstance(error.detail, dict) else {}
    inner = detail.get("error") if isinstance(detail.get("error"), dict) else {}
    status = int(inner.get("status") or error.status_code or 502)
    return RemoteError(str(inner.get("message") or error.message), status=status if status else 502, code=str(inner.get("code") or error.code or "remote_error"), details=codec.decode(inner.get("details")))
