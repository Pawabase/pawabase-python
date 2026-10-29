"""The data model: resources (tables), their schema and migrations, policies, schemas, transformers, custom routes, and the database."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ._base import Definitions, Service, seg


class Resources(Definitions):
    """Resources: the tables your API serves, with fields, relations, per-operation policies and indexes.

        pb.manage.resources.create({"name": "posts", "fields": [{"name": "title", "type": "string", "required": True}],
                                    "operations": {"list": {"enabled": True, "policy": "public"}}})
        pb.manage.resources.migrate("posts")
    """

    kind = "resources"

    # records, as an operator (policies do not apply)
    def records(self, name: str, *, limit: int | None = None, offset: int | None = None, **filters: Any) -> Any:
        """Browse a resource's records (``filter[field]=…`` as keyword filters are passed as given)."""
        return self.get(seg(self.kind, name, "records"), limit=limit, offset=offset, **filters)

    def insert(self, name: str, values: Mapping[str, Any]) -> Any:
        return self.post(seg(self.kind, name, "records"), dict(values))

    def update_record(self, name: str, record_id: Any, values: Mapping[str, Any]) -> Any:
        return self.patch(seg(self.kind, name, "records", record_id), dict(values))

    def delete_record(self, name: str, record_id: Any) -> Any:
        return self.delete(seg(self.kind, name, "records", record_id))

    def export(self, name: str, *, format: str | None = None) -> Any:
        """Every record, for a file download."""
        return self.get(seg(self.kind, name, "export"), format=format)

    def import_records(self, name: str, rows: Sequence[Mapping[str, Any]], *, key: str | None = None, mode: str | None = None, dry_run: bool | None = None) -> Any:
        """Add or update records from parsed rows. ``{created, updated, failed, errors}``."""
        body = {k: v for k, v in {"rows": list(rows), "key": key, "mode": mode, "dry_run": dry_run}.items() if v is not None}
        return self.post(seg(self.kind, name, "import"), body)

    def openapi(self, name: str) -> Any:
        """The resource's compiled routes."""
        return self.get(seg(self.kind, name, "openapi"))

    # table and schema
    def migrate(self, name: str) -> Any:
        """Create the resource's table, or add what its definition gained. Returns the statements it ran."""
        return self.post(seg(self.kind, name, "migrate"))

    def schema_plan(self, name: str) -> Any:
        """What applying the definition would change."""
        return self.get(seg(self.kind, name, "schema"))

    def schema_apply(self, name: str, body: Mapping[str, Any] | None = None) -> Any:
        return self.post(seg(self.kind, name, "schema", "apply"), dict(body or {}))

    def schema_acknowledge(self, name: str, body: Mapping[str, Any] | None = None) -> Any:
        """Take the table as it is now as the starting point and put what changed in the definition."""
        return self.post(seg(self.kind, name, "schema", "acknowledge"), dict(body or {}))

    def schema_purge(self, name: str, body: Mapping[str, Any] | None = None) -> Any:
        """Remove what migrations kept for a rollback."""
        return self.post(seg(self.kind, name, "schema", "purge"), dict(body or {}))

    def migrations(self, name: str) -> Any:
        return self.get(seg(self.kind, name, "migrations"))

    def rollback_migration(self, name: str, migration_id: str) -> Any:
        return self.post(seg(self.kind, name, "migrations", migration_id, "rollback"))

    def indexes(self, name: str) -> Any:
        """A table's indexes: declared, field, hand-made and system."""
        return self.get(seg(self.kind, name, "indexes"))

    def drop_index(self, name: str, index: str, body: Mapping[str, Any] | None = None) -> Any:
        """Drop an index made outside Pawabase."""
        return self.post(seg(self.kind, name, "indexes", index, "drop"), dict(body or {}))


class Schema(Service):
    """The whole database against its definitions."""

    def overview(self) -> Any:
        """Every resource's table against its definition."""
        return self.get("/schema")

    def unmapped_tables(self) -> Any:
        """Tables in the database that no resource is defined for."""
        return self.get("/schema/tables")


class Policies(Definitions):
    """Named access policies (conditions) that resources, buckets, channels and functions refer to."""

    kind = "policies"


class Schemas(Definitions):
    """Reusable data shapes referred to by resource fields."""

    kind = "schemas"


class Transformers(Definitions):
    """Transformers that reshape a field's value on the way out (hide, mask, format)."""

    kind = "transformers"


class Routes(Definitions):
    """Custom HTTP routes under ``/rest`` that run a flow."""

    kind = "routes"


class Database(Service):
    """The environment's database: tables, rows and the SQL console (one statement at a time; reads are read-only in the database itself)."""

    def info(self) -> Any:
        return self.get("/database")

    def table(self, table: str) -> Any:
        """A table's columns, indexes and foreign keys."""
        return self.get(seg("database", "tables", table))

    def rows(self, table: str, *, limit: int | None = None, offset: int | None = None, sort: str | None = None, **filters: Any) -> Any:
        return self.get(seg("database", "tables", table, "rows"), limit=limit, offset=offset, sort=sort, **filters)

    def query(self, sql: str, params: Sequence[Any] | None = None, *, allow_write: bool = False) -> Any:
        """Run one statement. A write needs ``allow_write=True``; it is audited and clears the environment's caches."""
        return self.post("/database/query", {"sql": sql, "params": list(params or []), "allow_write": allow_write})


class Runtime(Service):
    """The runtime capabilities a function sees, called remotely (what ``pawabase emulate`` uses)."""

    def call(self, method: str, args: Sequence[Any] = (), kwargs: Mapping[str, Any] | None = None, *, as_user: Mapping[str, Any] | None = None, branch: str | None = None) -> Any:
        return self.post("/runtime/call", {"method": method, "args": list(args), "kwargs": dict(kwargs or {}), "as_user": dict(as_user) if as_user else None, "branch": branch})

    def sql(self, op: str, **fields: Any) -> Any:
        """``fetch``, ``insert``, ``execute``, ``begin``, ``commit``…, optionally inside a transaction (``tx``)."""
        return self.post("/runtime/db", {"op": op, **fields})

    def identify(self, token: str | None) -> Any:
        """Who a user access token belongs to."""
        return self.post("/runtime/identify", {"token": token})
