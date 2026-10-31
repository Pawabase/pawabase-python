"""Records: ``pb.from_("posts")`` reads and writes a resource under its policies.

A query is built with chained, immutable methods and run with ``execute()``; the blocking and the async client differ only in whether a method
returns a value or an awaitable:

    page = pb.from_("posts").eq("status", "live").gte("views", 10).order("created_at", "desc").select("id", "title").per_page(20).execute()
    for post in pb.from_("posts").eq("status", "live").iterate():          # blocking: every row, page after page
        ...
    async for post in apb.from_("posts").iterate(): ...                     # async

The server applies one filter per field and keeps the last when a field repeats, so a second filter on a field raises instead of silently
dropping one. Booleans and ``None`` go through ``is_``/``is_not`` (``eq`` does that for you).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import decimal
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import _core
from .errors import NotFoundError, ValidationError

OPERATORS = ("eq", "neq", "gt", "gte", "lt", "lte", "like", "ilike", "in", "nin", "is", "isnot")


class QueryError(ValidationError):
    """A query that cannot be sent as written (said before any request is made)."""

    def __init__(self, message: str) -> None:
        super().__init__(400, {"error": "invalid_query", "message": message})


@dataclass
class Page:
    """One page of a list: ``data`` rows, the ``page`` and ``per_page`` asked for, and the ``total`` (``None`` when a per-row policy means the server cannot count)."""

    data: list[dict[str, Any]]
    page: int = 1
    per_page: int = 20
    total: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.data)

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self.data[index]

    @property
    def has_next(self) -> bool:
        if self.total is not None:
            return self.page * self.per_page < self.total
        return len(self.data) >= self.per_page

    @classmethod
    def of(cls, body: Any, page: int, per_page: int) -> Page:
        if isinstance(body, list):
            return cls(body, page, per_page, len(body))
        body = body or {}
        return cls(list(body.get("data") or []), int(body.get("page") or page), int(body.get("per_page") or per_page), body.get("total"), body)


def _format(value: Any, op: str, column: str) -> str:
    if value is None:
        raise QueryError(f"Filter on {column}: None needs is_()/is_not(), not {op}.")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, (int, float, decimal.Decimal, str)):
        return str(value)
    return str(value)


def encode_filter(column: str, op: str, value: Any) -> tuple[str, str] | str:
    """``(op, text)`` for the wire, ``"empty"`` for an ``in`` with no values (matches nothing), or ``"skip"`` for a ``nin`` with none."""
    if op not in OPERATORS:
        raise QueryError(f"Unknown filter operator {op!r}; use one of {', '.join(OPERATORS)}.")
    if op in ("eq", "neq"):
        if value is None:
            return ("is" if op == "eq" else "isnot", "null")
        if isinstance(value, bool):  # ``eq.false`` would match true rows on the server
            return ("is" if op == "eq" else "isnot", "true" if value else "false")
        return (op, _format(value, op, column))
    if op in ("is", "isnot"):
        if value is not None and not isinstance(value, bool):
            raise QueryError(f"Filter on {column}: {op} takes None, True or False.")
        return (op, "null" if value is None else ("true" if value else "false"))
    if op in ("in", "nin"):
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence | set | frozenset):
            raise QueryError(f"Filter on {column}: {op} takes a list.")
        if not value:
            return "empty" if op == "in" else "skip"
        parts = [_format(v, op, column) for v in value]
        bad = next((p for p in parts if "," in p), None)
        if bad is not None:
            raise QueryError(f"Filter on {column}: the value {bad!r} contains a comma, and the server splits {op} lists on commas.")
        return (op, ",".join(parts))
    if op in ("like", "ilike"):
        if not isinstance(value, str):
            raise QueryError(f"Filter on {column}: {op} takes a string pattern.")
        return (op, value)
    return (op, _format(value, op, column))


class _Query:
    """The immutable state of a query and the methods that build it; subclasses run it."""

    def __init__(self, client: Any, resource: str, state: dict[str, Any] | None = None) -> None:
        self._client = client
        self.resource = resource
        self._s: dict[str, Any] = state or {"filters": {}, "sort": [], "select": None, "expand": [], "page": 1, "per_page": None, "empty": False}

    def _with(self, **change: Any) -> Any:
        state = {**self._s, "filters": dict(self._s["filters"]), "sort": list(self._s["sort"]), "expand": list(self._s["expand"]), **change}
        return type(self)(self._client, self.resource, state)

    # ── filters ──────────────────────────────────────────────────────────

    def filter(self, column: str, op: str, value: Any) -> Any:
        """Add a filter: ``filter("views", "gte", 10)``. One per field."""
        if column in self._s["filters"]:
            raise QueryError(
                f'There is already a filter on "{column}". The server applies one filter per field and drops the others, so a range with two '
                "bounds on one field cannot be written as a single list request."
            )
        encoded = encode_filter(column, op, value)
        filters = dict(self._s["filters"])
        if encoded == "empty":
            return self._with(empty=True)
        if encoded == "skip":
            return self._with()
        filters[column] = f"{encoded[0]}.{encoded[1]}"
        return self._with(filters=filters)

    def eq(self, column: str, value: Any) -> Any:
        return self.filter(column, "eq", value)

    def neq(self, column: str, value: Any) -> Any:
        return self.filter(column, "neq", value)

    def gt(self, column: str, value: Any) -> Any:
        return self.filter(column, "gt", value)

    def gte(self, column: str, value: Any) -> Any:
        return self.filter(column, "gte", value)

    def lt(self, column: str, value: Any) -> Any:
        return self.filter(column, "lt", value)

    def lte(self, column: str, value: Any) -> Any:
        return self.filter(column, "lte", value)

    def like(self, column: str, pattern: str) -> Any:
        """Pattern match: ``*`` and ``%`` match any run of characters."""
        return self.filter(column, "like", pattern)

    def ilike(self, column: str, pattern: str) -> Any:
        """Case-insensitive on PostgreSQL; the same as :meth:`like` elsewhere."""
        return self.filter(column, "ilike", pattern)

    def in_(self, column: str, values: Sequence[Any]) -> Any:
        """Any of *values* (none may contain a comma). An empty list matches nothing."""
        return self.filter(column, "in", values)

    def not_in(self, column: str, values: Sequence[Any]) -> Any:
        return self.filter(column, "nin", values)

    def is_(self, column: str, value: bool | None) -> Any:
        """``None``, ``True`` or ``False``: the right way to test booleans and nulls."""
        return self.filter(column, "is", value)

    def is_not(self, column: str, value: bool | None) -> Any:
        return self.filter(column, "isnot", value)

    def where(self, conditions: Mapping[str, Any] | None = None, /, **fields: Any) -> Any:
        """Several filters at once: ``where(status="live", views={"gte": 10})`` or ``where({"status": "live"})``."""
        query = self
        for column, condition in {**(conditions or {}), **fields}.items():
            if isinstance(condition, Mapping):
                if len(condition) != 1:
                    raise QueryError(f'where(): "{column}" has {len(condition)} operators; the server takes one filter per field.')
                ((op, value),) = condition.items()
                query = query.filter(column, op, value)
            else:
                query = query.filter(column, "eq", condition)
        return query

    # ── shape ────────────────────────────────────────────────────────────

    def order(self, column: str, direction: str = "asc") -> Any:
        """Sort by *column*; ``order("-created_at")`` and ``order("created_at", "desc")`` are the same. Call again to sort by more."""
        desc = direction.lower() == "desc" or column.startswith("-")
        return self._with(sort=[*self._s["sort"], ("-" if desc else "") + column.lstrip("+-")])

    def select(self, *columns: str) -> Any:
        """Only these fields."""
        return self._with(select=list(columns))

    def expand(self, *relations: str) -> Any:
        """Embed related records: ``expand("author", "comments")``."""
        return self._with(expand=[*self._s["expand"], *relations])

    def page(self, number: int) -> Any:
        return self._with(page=number)

    def per_page(self, count: int) -> Any:
        return self._with(per_page=count)

    limit = per_page

    def _params(self) -> dict[str, Any]:
        s = self._s
        params: dict[str, Any] = {f"filter[{column}]": value for column, value in s["filters"].items()}
        if s["sort"]:
            params["sort"] = ",".join(s["sort"])
        if s["select"]:
            params["select"] = ",".join(s["select"])
        if s["expand"]:
            params["expand"] = ",".join(dict.fromkeys(s["expand"]))
        if s["page"] != 1:
            params["page"] = s["page"]
        if s["per_page"] is not None:
            params["per_page"] = s["per_page"]
        return params

    def _page_of(self, body: Any) -> Page:
        return Page.of(body, self._s["page"], self._s["per_page"] or 20)

    def _fetch(self) -> Any:
        if self._s["empty"]:
            return Page([], self._s["page"], self._s["per_page"] or 20, 0)
        return _core.then(self._client._call("GET", f"/rest/v1/{_core.key_path(self.resource)}", params=self._params() or None, retry=True), self._page_of)

    def _count_of(self, page: Page) -> int:
        if page.total is None:
            raise QueryError(f"The server cannot count {self.resource} for this caller: a per-row policy makes the total unknown. Fetch the rows and count them.")
        return page.total

    def _single_of(self, row: Any) -> dict[str, Any]:
        if row is None:
            raise NotFoundError(404, {"error": "not_found", "message": f"No {self.resource} record matches."})
        return row


class Query(_Query):
    """A query on the blocking client."""

    def execute(self) -> Page:
        """Run the query: one page."""
        return self._fetch()

    def first(self) -> dict[str, Any] | None:
        page = self.per_page(1).page(1).execute()
        return page.data[0] if page.data else None

    def single(self) -> dict[str, Any]:
        """The one matching record; :class:`~pawabase.errors.NotFoundError` when there is none."""
        return self._single_of(self.first())

    def count(self) -> int:
        return self._count_of(self.per_page(1).page(1).execute())

    def exists(self) -> bool:
        return self.first() is not None

    def pages(self, *, page_size: int | None = None, max_records: int | None = None) -> Iterator[Page]:
        """Every page in turn, until the last (or *max_records*)."""
        query = self.per_page(page_size or self._s["per_page"] or 100)
        seen = 0
        while True:
            page = query.execute()
            yield page
            seen += len(page.data)
            if (max_records is not None and seen >= max_records) or not page.data or not page.has_next:
                return
            query = query.page(page.page + 1)

    def iterate(self, *, page_size: int | None = None, max_records: int | None = None) -> Iterator[dict[str, Any]]:
        """Every row, page after page."""
        count = 0
        for page in self.pages(page_size=page_size, max_records=max_records):
            for row in page.data:
                if max_records is not None and count >= max_records:
                    return
                count += 1
                yield row

    def all(self, *, page_size: int | None = None, max_records: int | None = None) -> list[dict[str, Any]]:
        return list(self.iterate(page_size=page_size, max_records=max_records))

    __iter__ = iterate


class AsyncQuery(_Query):
    """A query on the async client: the same methods, awaited."""

    async def execute(self) -> Page:
        return await self._fetch()

    async def first(self) -> dict[str, Any] | None:
        page = await self.per_page(1).page(1).execute()
        return page.data[0] if page.data else None

    async def single(self) -> dict[str, Any]:
        return self._single_of(await self.first())

    async def count(self) -> int:
        return self._count_of(await self.per_page(1).page(1).execute())

    async def exists(self) -> bool:
        return (await self.first()) is not None

    async def pages(self, *, page_size: int | None = None, max_records: int | None = None) -> AsyncIterator[Page]:
        query = self.per_page(page_size or self._s["per_page"] or 100)
        seen = 0
        while True:
            page = await query.execute()
            yield page
            seen += len(page.data)
            if (max_records is not None and seen >= max_records) or not page.data or not page.has_next:
                return
            query = query.page(page.page + 1)

    async def iterate(self, *, page_size: int | None = None, max_records: int | None = None) -> AsyncIterator[dict[str, Any]]:
        count = 0
        async for page in self.pages(page_size=page_size, max_records=max_records):
            for row in page.data:
                if max_records is not None and count >= max_records:
                    return
                count += 1
                yield row

    async def all(self, *, page_size: int | None = None, max_records: int | None = None) -> list[dict[str, Any]]:
        return [row async for row in self.iterate(page_size=page_size, max_records=max_records)]

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self.iterate()


def _expand_params(expand: Sequence[str] | None) -> dict[str, Any] | None:
    return {"expand": ",".join(expand)} if expand else None


class _ResourceBase:
    query_class: type[_Query] = Query

    def __init__(self, client: Any, name: str) -> None:
        self._client = client
        self.name = name

    def _path(self, record_id: Any = None) -> str:
        base = f"/rest/v1/{_core.key_path(self.name)}"
        return base if record_id is None else f"{base}/{_core.path_of(record_id)}"

    def similar(
        self, *, text: str | None = None, vector: Sequence[float] | None = None, field: str | None = None, metric: str = "cosine", limit: int = 10,
        max_distance: float | None = None,
    ) -> Any:
        """Records closest in meaning, closest first, each with a ``distance`` (smaller is closer).

        Give ``text`` (turned into a vector by the field's embedding model) or a ``vector``. Vector fields are left out of the records.
        """
        body = _core.compact({"text": text, "vector": list(vector) if vector is not None else None, "field": field, "metric": metric, "limit": limit, "max_distance": max_distance})
        return self._client._call("POST", f"{self._path()}/similar", json=body)

    def query(self) -> Any:
        return self.query_class(self._client, self.name)

    # Every query method, started on a fresh query: ``pb.from_("posts").eq(...)``.
    def filter(self, column: str, op: str, value: Any) -> Any:
        return self.query().filter(column, op, value)

    def where(self, conditions: Mapping[str, Any] | None = None, /, **fields: Any) -> Any:
        return self.query().where(conditions, **fields)

    def eq(self, column: str, value: Any) -> Any:
        return self.query().eq(column, value)

    def neq(self, column: str, value: Any) -> Any:
        return self.query().neq(column, value)

    def gt(self, column: str, value: Any) -> Any:
        return self.query().gt(column, value)

    def gte(self, column: str, value: Any) -> Any:
        return self.query().gte(column, value)

    def lt(self, column: str, value: Any) -> Any:
        return self.query().lt(column, value)

    def lte(self, column: str, value: Any) -> Any:
        return self.query().lte(column, value)

    def like(self, column: str, pattern: str) -> Any:
        return self.query().like(column, pattern)

    def ilike(self, column: str, pattern: str) -> Any:
        return self.query().ilike(column, pattern)

    def in_(self, column: str, values: Sequence[Any]) -> Any:
        return self.query().in_(column, values)

    def not_in(self, column: str, values: Sequence[Any]) -> Any:
        return self.query().not_in(column, values)

    def is_(self, column: str, value: bool | None) -> Any:
        return self.query().is_(column, value)

    def is_not(self, column: str, value: bool | None) -> Any:
        return self.query().is_not(column, value)

    def order(self, column: str, direction: str = "asc") -> Any:
        return self.query().order(column, direction)

    def select(self, *columns: str) -> Any:
        return self.query().select(*columns)

    def expand(self, *relations: str) -> Any:
        return self.query().expand(*relations)

    def page(self, number: int) -> Any:
        return self.query().page(number)

    def per_page(self, count: int) -> Any:
        return self.query().per_page(count)

    limit = per_page

    def execute(self) -> Any:
        """The first page of every record."""
        return self.query().execute()

    def list(self, *, where: Mapping[str, Any] | None = None, sort: str | Sequence[str] | None = None, select: Sequence[str] | None = None,
             expand: Sequence[str] | None = None, page: int | None = None, per_page: int | None = None) -> Any:
        """One page with everything in one call: ``list(where={"status": "live"}, sort="-created_at", per_page=20)``."""
        query = self.query().where(where or {})
        for spec in ([s.strip() for s in sort.split(",")] if isinstance(sort, str) else list(sort or [])):
            if spec:
                query = query.order(spec)
        if select:
            query = query.select(*select)
        if expand:
            query = query.expand(*expand)
        if page:
            query = query.page(page)
        if per_page:
            query = query.per_page(per_page)
        return query.execute()

    # ── one record ───────────────────────────────────────────────────────

    def get(self, record_id: Any, *, expand: Sequence[str] | None = None) -> Any:
        """One record by id; :class:`~pawabase.errors.NotFoundError` when it does not exist or the policy hides it."""
        return self._client._call("GET", self._path(record_id), params=_expand_params(expand), retry=True)

    def create(self, **values: Any) -> Any:
        """Create a record with named fields: ``posts.create(title="Hello", views=0)``."""
        return self._client._call("POST", self._path(), json=_json(values))

    def update(self, record_id: Any, **values: Any) -> Any:
        """Change some fields (PATCH). A required field cannot be set to ``None``."""
        return self._client._call("PATCH", self._path(record_id), json=_json(values))

    def replace(self, record_id: Any, **values: Any) -> Any:
        """Replace the record (PUT)."""
        return self._client._call("PUT", self._path(record_id), json=_json(values))

    def delete(self, record_id: Any) -> Any:
        return self._client._call("DELETE", self._path(record_id))


def _json(values: Mapping[str, Any]) -> dict[str, Any]:
    """Values the wire can carry: dates as ISO text, decimals as text."""
    def one(value: Any) -> Any:
        if isinstance(value, (dt.datetime, dt.date)):
            return value.isoformat()
        if isinstance(value, decimal.Decimal):
            return str(value)
        if isinstance(value, Mapping):
            return {k: one(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [one(v) for v in value]
        return value

    return {k: one(v) for k, v in values.items()}


@dataclass
class CreateManyResult:
    created: list[dict[str, Any]] = field(default_factory=list)
    failed: list[tuple[int, Exception]] = field(default_factory=list)


class Resource(_ResourceBase):
    """A resource on the blocking client."""

    query_class = Query

    def maybe_get(self, record_id: Any, *, expand: Sequence[str] | None = None) -> dict[str, Any] | None:
        """Like :meth:`get`, but ``None`` instead of an error when there is no such record."""
        try:
            return self.get(record_id, expand=expand)
        except NotFoundError:
            return None

    def create_many(self, rows: Sequence[Mapping[str, Any]], *, stop_on_error: bool = False) -> CreateManyResult:
        """Create each row in turn; failures are collected (or stop the run with *stop_on_error*)."""
        result = CreateManyResult()
        for index, row in enumerate(rows):
            try:
                result.created.append(self.create(**dict(row)))
            except Exception as error:  # noqa: BLE001 - one bad row must not hide the others
                result.failed.append((index, error))
                if stop_on_error:
                    break
        return result

    def iterate(self, **options: Any) -> Iterator[dict[str, Any]]:
        return self.query().iterate(**options)

    def all(self, **options: Any) -> list[dict[str, Any]]:
        return self.query().all(**options)

    def first(self) -> dict[str, Any] | None:
        return self.query().first()

    def count(self) -> int:
        return self.query().count()

    def single(self) -> dict[str, Any]:
        return self.query().single()

    def exists(self) -> bool:
        return self.query().exists()


class AsyncResource(_ResourceBase):
    """A resource on the async client."""

    query_class = AsyncQuery

    async def maybe_get(self, record_id: Any, *, expand: Sequence[str] | None = None) -> dict[str, Any] | None:
        try:
            return await self.get(record_id, expand=expand)
        except NotFoundError:
            return None

    async def create_many(self, rows: Sequence[Mapping[str, Any]], *, concurrency: int = 5, stop_on_error: bool = False) -> CreateManyResult:
        """Create the rows, *concurrency* at a time."""
        result = CreateManyResult()
        slots: dict[int, dict[str, Any]] = {}
        gate = asyncio.Semaphore(max(1, concurrency))
        stop = False

        async def one(index: int, row: Mapping[str, Any]) -> None:
            nonlocal stop
            async with gate:
                if stop:
                    return
                try:
                    slots[index] = await self.create(**dict(row))
                except Exception as error:  # noqa: BLE001
                    result.failed.append((index, error))
                    stop = stop or stop_on_error

        await asyncio.gather(*(one(i, r) for i, r in enumerate(rows)))
        result.created = [slots[i] for i in sorted(slots)]
        result.failed.sort(key=lambda item: item[0])
        return result

    def iterate(self, **options: Any) -> AsyncIterator[dict[str, Any]]:
        return self.query().iterate(**options)

    async def all(self, **options: Any) -> list[dict[str, Any]]:
        return await self.query().all(**options)

    async def first(self) -> dict[str, Any] | None:
        return await self.query().first()

    async def count(self) -> int:
        return await self.query().count()

    async def single(self) -> dict[str, Any]:
        return await self.query().single()

    async def exists(self) -> bool:
        return await self.query().exists()
