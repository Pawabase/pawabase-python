"""Testing functions without a Pawabase, a network or a database server.

::

    from pawabase.testing import FakeRuntime, call

    async def test_reserve_stock():
        runtime = FakeRuntime(sql="CREATE TABLE stock (id INTEGER PRIMARY KEY, sku TEXT, on_hand INTEGER)")
        await (await runtime.db()).insert("stock", {"sku": "A", "on_hand": 3})
        result = await call("stock.reserve", {"sku": "A", "n": 2}, runtime=runtime, project_dir="functions")
        assert result.ok and result.result == {"left": 1}
        assert runtime.emitted == [("stock.reserved", {"sku": "A", "n": 2})]

:class:`FakeRuntime` is a complete ``ctx.runtime``: an in-memory resource store, **real SQL** on an in-memory SQLite (so a conditional ``UPDATE … WHERE stock >= ?``
behaves as it does in production), a cache, and recorders for everything that leaves the function: events, queued flows and functions, realtime messages, mail,
outbound HTTP, storage writes. Nothing it does touches a network.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .functions import FunctionError, FunctionSpec, get_exact, load_functions
from .invoke import Invocation, invoke


class FakeDb:
    """The SQL half of the runtime, on SQLite. ``?`` placeholders, as on the platform."""

    def __init__(self, path: str = ":memory:", sql: str = "") -> None:
        self.connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = asyncio.Lock()
        self._depth = 0
        if sql:
            self.connection.executescript(sql)

    @staticmethod
    def _param(value: Any) -> Any:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (dict, list)):
            return json.dumps(value, default=str)
        if isinstance(value, dt.datetime):
            return value.isoformat(sep=" ")
        return value

    async def fetch(self, statement: str, params: list[Any] | tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cursor = self.connection.execute(statement, [self._param(p) for p in params])
        return [dict(row) for row in cursor.fetchall()]

    async def one(self, statement: str, params: list[Any] | tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = await self.fetch(statement, params)
        return rows[0] if rows else None

    async def scalar(self, statement: str, params: list[Any] | tuple[Any, ...] = (), default: Any = None) -> Any:
        row = await self.one(statement, params)
        if not row:
            return default
        value = next(iter(row.values()))
        return default if value is None else value

    async def execute(self, statement: str, params: list[Any] | tuple[Any, ...] = ()) -> int:
        return self.connection.execute(statement, [self._param(p) for p in params]).rowcount

    async def insert(self, table: str, data: Mapping[str, Any]) -> dict[str, Any]:
        values = dict(data)
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cursor = self.connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", [self._param(v) for v in values.values()])
        row = await self.one(f"SELECT * FROM {table} WHERE rowid = ?", [cursor.lastrowid])
        return row or values

    async def update(self, table: str, record_id: Any, data: Mapping[str, Any]) -> int:
        sets = ", ".join(f"{k} = ?" for k in data)
        return self.connection.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*(self._param(v) for v in data.values()), record_id]).rowcount

    async def delete(self, table: str, record_id: Any) -> int:
        return self.connection.execute(f"DELETE FROM {table} WHERE id = ?", [record_id]).rowcount

    @staticmethod
    def now() -> dt.datetime:
        return dt.datetime.now(dt.UTC)


class _Transaction:
    def __init__(self, db: FakeDb) -> None:
        self.db = db

    async def __aenter__(self) -> FakeDb:
        await self.db._lock.acquire()
        self.db.connection.execute("BEGIN")
        return self.db

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            self.db.connection.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db._lock.release()
        return False


class FakeRuntime:
    """A ``ctx.runtime`` that does everything in memory and remembers what the function asked for.

    Attributes you can assert on: ``emitted``, ``flows`` (queued), ``flow_calls``, ``functions`` (queued), ``published``, ``mails``, ``http_calls``, ``stored``,
    ``metrics``, ``logs``, ``cache``, ``records`` (``{resource: {id: row}}``).

    Args:
        sql: SQL run once to create the schema for ``db()``.
        secrets: ``name -> value`` for ``secret()``.
        http: ``(method, url) -> response`` mapping, or a callable ``(method, url, kwargs) -> {"status", "body"}``; anything else answers 200 ``{}``.
        flows: ``name -> callable(input)`` results for ``call_flow``.
        functions: ``name -> callable(input)`` for ``call_function`` (a coroutine function is awaited).
        users: ``user_id -> profile`` for ``identity_user``.
        policy: ``callable(ref, context) -> bool`` for ``check_policy`` (allow everything by default).
    """

    def __init__(
        self,
        *,
        sql: str = "",
        secrets: Mapping[str, str] | None = None,
        http: Mapping[tuple[str, str], Any] | Callable[..., Any] | None = None,
        flows: Mapping[str, Callable[[Any], Any]] | None = None,
        functions: Mapping[str, Callable[[Any], Any]] | None = None,
        users: Mapping[str, Mapping[str, Any]] | None = None,
        policy: Callable[[Any, Mapping[str, Any]], bool] | None = None,
    ) -> None:
        self._db = FakeDb(sql=sql)
        self.secrets = dict(secrets or {})
        self._http = http
        self._flows = dict(flows or {})
        self._functions = dict(functions or {})
        self._users = dict(users or {})
        self._policy = policy
        self.records: dict[str, dict[Any, dict[str, Any]]] = {}
        self.cache: dict[str, Any] = {}
        self.emitted: list[tuple[str, Any]] = []
        self.flows: list[tuple[str, Any]] = []
        self.flow_calls: list[tuple[str, Any]] = []
        self.functions: list[tuple[str, Any]] = []
        self.published: list[tuple[str, str, Any]] = []
        self.mails: list[dict[str, Any]] = []
        self.http_calls: list[dict[str, Any]] = []
        self.stored: dict[tuple[str, str], bytes] = {}
        self.metrics: list[tuple[str, float, Mapping[str, str]]] = []
        self.logs: list[dict[str, Any]] = []
        self.webhooks: list[tuple[str, Any]] = []

    # data
    async def resource_list(self, resource: str, *, filters: Mapping[str, Any] | None = None, sort: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        rows = [row for row in self.records.get(resource, {}).values() if all(row.get(k) == v for k, v in (filters or {}).items())]
        if sort:
            key = sort.lstrip("-")
            rows.sort(key=lambda row: (row.get(key) is None, row.get(key)), reverse=sort.startswith("-"))
        return {"data": copy.deepcopy(rows[offset : offset + limit]), "total": len(rows), "limit": limit, "offset": offset}

    async def resource_get(self, resource: str, record_id: Any) -> dict[str, Any] | None:
        row = self.records.get(resource, {}).get(str(record_id))
        return copy.deepcopy(row) if row else None

    async def resource_create(self, resource: str, data: Mapping[str, Any]) -> dict[str, Any]:
        table = self.records.setdefault(resource, {})
        row = {"id": str(data.get("id") or len(table) + 1), **copy.deepcopy(dict(data))}
        row["id"] = str(row["id"])
        table[row["id"]] = row
        return copy.deepcopy(row)

    async def resource_update(self, resource: str, record_id: Any, data: Mapping[str, Any]) -> dict[str, Any] | None:
        row = self.records.get(resource, {}).get(str(record_id))
        if row is None:
            return None
        row.update(copy.deepcopy(dict(data)))
        return copy.deepcopy(row)

    async def resource_delete(self, resource: str, record_id: Any) -> bool:
        return self.records.get(resource, {}).pop(str(record_id), None) is not None

    async def db_query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        return await self._db.fetch(sql, params or [])

    async def db_transaction(self, operations: list[Mapping[str, Any]]) -> list[Any]:
        results: list[Any] = []
        for op in operations:
            kind = op.get("op")
            if kind == "create":
                results.append(await self.resource_create(op["resource"], op.get("data") or {}))
            elif kind == "update":
                results.append(await self.resource_update(op["resource"], op["id"], op.get("data") or {}))
            elif kind == "delete":
                results.append(await self.resource_delete(op["resource"], op["id"]))
        return results

    async def db(self) -> FakeDb:
        return self._db

    def transaction(self) -> _Transaction:
        return _Transaction(self._db)

    # cache
    async def cache_get(self, key: str) -> Any:
        return self.cache.get(key)

    async def cache_set(self, key: str, value: Any, ttl: int | None = None, tags: list[str] | None = None) -> None:
        self.cache[key] = value

    async def cache_delete(self, key: str) -> bool:
        return self.cache.pop(key, None) is not None

    async def cache_invalidate(self, tags: list[str]) -> int:
        return 0

    # events, flows, functions, realtime
    async def emit(self, name: str, payload: Any) -> str:
        self.emitted.append((name, copy.deepcopy(payload)))
        return uuid.uuid4().hex

    async def dispatch_flow(self, flow: str, input: Any, *, delay: int = 0, queue: str | None = None) -> str:
        self.flows.append((flow, copy.deepcopy(input)))
        return uuid.uuid4().hex

    async def call_flow(self, flow: str, input: Any) -> Any:
        self.flow_calls.append((flow, copy.deepcopy(input)))
        handler = self._flows.get(flow)
        if handler is None:
            raise FunctionError(f"no flow {flow!r}", status=404, code="unknown_flow")
        result = handler(input)
        return await result if asyncio.iscoroutine(result) else result

    async def dispatch_function(self, function: str, input: Any, *, delay: int = 0, queue: str | None = None) -> str:
        self.functions.append((function, copy.deepcopy(input)))
        return uuid.uuid4().hex

    async def call_function(self, name: str, input: Any) -> Any:
        handler = self._functions.get(name)
        if handler is None:
            raise FunctionError(f"no function {name!r}", status=404, code="unknown_function")
        result = handler(input)
        return await result if asyncio.iscoroutine(result) else result

    async def publish(self, channel: str, event: str, payload: Any) -> dict[str, Any]:
        self.published.append((channel, event, copy.deepcopy(payload)))
        return {"delivered": 0}

    # storage and mail
    async def storage_put(self, bucket: str, key: str, content: bytes, content_type: str = "") -> dict[str, Any]:
        self.stored[(bucket, key)] = bytes(content)
        return {"bucket": bucket, "key": key, "size": len(content), "content_type": content_type}

    async def storage_read(self, bucket: str, key: str, limit: int = 1_048_576) -> bytes:
        try:
            return self.stored[(bucket, key)][:limit]
        except KeyError:
            raise FunctionError(f"no object {bucket}/{key}", status=404, code="not_found") from None

    async def storage_signed_url(self, bucket: str, key: str, method: str = "GET", expires_in: int = 300) -> str:
        return f"https://storage.test/{bucket}/{key}?method={method}&expires={expires_in}"

    async def storage_delete(self, bucket: str, key: str) -> bool:
        return self.stored.pop((bucket, key), None) is not None

    async def send_mail(self, to: list[str], subject: str, *, text: str | None = None, html: str | None = None, template: str | None = None, data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self.mails.append({"to": list(to), "subject": subject, "text": text, "html": html, "template": template, "data": dict(data or {})})
        return {"queued": True}

    # outbound
    async def http_request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None, json: Any = None, params: Mapping[str, Any] | None = None, timeout: float = 30.0, retries: int = 0) -> dict[str, Any]:
        call = {"method": method.upper(), "url": url, "headers": dict(headers or {}), "json": json, "params": dict(params or {})}
        self.http_calls.append(call)
        answer: Any = {"status": 200, "headers": {}, "body": {}}
        if callable(self._http):
            answer = self._http(method.upper(), url, call)
            answer = await answer if asyncio.iscoroutine(answer) else answer
        elif self._http and (method.upper(), url) in self._http:
            answer = self._http[(method.upper(), url)]
        if isinstance(answer, Mapping):
            return {"status": 200, "headers": {}, "body": {}, **answer}
        return {"status": 200, "headers": {}, "body": answer}

    async def webhook_send(self, event: str, payload: Any) -> int:
        self.webhooks.append((event, payload))
        return 0

    # platform
    async def secret(self, name: str) -> str | None:
        return self.secrets.get(name)

    async def identity_user(self, user_id: str) -> dict[str, Any] | None:
        return self._users.get(str(user_id))

    async def check_policy(self, ref: Any, context: Mapping[str, Any]) -> bool:
        return True if self._policy is None else bool(self._policy(ref, context))

    async def log(self, level: str, message: str, data: Any = None) -> None:
        self.logs.append({"level": level, "message": message, "data": data})

    async def metric(self, name: str, value: float = 1.0, tags: Mapping[str, str] | None = None) -> None:
        self.metrics.append((name, value, dict(tags or {})))


def authenticated(user_id: str = "1", *, email: str | None = None, roles: list[str] | None = None, permissions: list[str] | None = None) -> dict[str, Any]:
    """An auth context for a signed-in caller, to pass as ``auth=``."""
    return {"authenticated": True, "user_id": user_id, "email": email, "roles": roles or [], "permissions": permissions or [], "kind": "user"}


async def call(
    name: str | FunctionSpec,
    input: Any = None,
    *,
    runtime: FakeRuntime | Any | None = None,
    auth: Mapping[str, Any] | None = None,
    project_dir: str | Path | None = None,
    project: str = "local",
    request: Mapping[str, Any] | None = None,
    trigger: str = "test",
) -> Invocation:
    """Run a function the way the platform would: validated input, timeout, errors as answers.

    Pass ``project_dir`` (the ``functions`` folder, or its parent) the first time to import the functions; later calls find them already registered.
    """
    if project_dir is not None:
        loaded = load_functions(project_dir, project=project)
        if loaded.errors:
            raise AssertionError("functions failed to import:\n" + "\n".join(loaded.errors))
    spec = name if isinstance(name, FunctionSpec) else get_exact(project, name)
    if spec is None:
        raise LookupError(f"no function {name!r} is registered for project {project!r}; pass project_dir= to load it")
    return await invoke(spec, input, runtime=runtime or FakeRuntime(), auth=auth, project=project, trigger=trigger, request=request)
