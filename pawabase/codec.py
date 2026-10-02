"""JSON that remembers types.

A function's data is not only JSON: SQL returns ``datetime``\\s and ``Decimal``\\s, storage returns bytes, a query parameter may be a ``set``. Plain JSON would
turn them into strings and the code on the other end would quietly receive different values than the code that sent them. When the emulator talks to a
deployment, both ends therefore tag the few types JSON lacks::

    {"$b64": "..."}   bytes        {"$dt": "2026-10-02T09:00:00+00:00"}   datetime
    {"$date": "..."}  date         {"$dec": "19.99"}                       Decimal
    {"$set": [...]}   set

Anything else that JSON cannot hold becomes its ``str``. A real object that happens to have one of these keys as its only key would be misread; no real
payload does, and the tags start with ``$`` for exactly that reason.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any


def encode(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$b64": base64.b64encode(bytes(value)).decode()}
    if isinstance(value, dt.datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, dt.date):
        return {"$date": value.isoformat()}
    if isinstance(value, Decimal):
        return {"$dec": str(value)}
    if isinstance(value, (set, frozenset)):
        return {"$set": [encode(v) for v in value]}
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


def decode(value: Any) -> Any:
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        if len(value) == 1:
            ((tag, inner),) = value.items()
            if tag == "$b64":
                return base64.b64decode(inner)
            if tag == "$dt":
                return dt.datetime.fromisoformat(inner)
            if tag == "$date":
                return dt.date.fromisoformat(inner)
            if tag == "$dec":
                return Decimal(inner)
            if tag == "$set":
                return {decode(v) for v in inner}
        return {k: decode(v) for k, v in value.items()}
    return value
