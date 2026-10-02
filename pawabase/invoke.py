"""Running one function the way the platform does.

The emulator, ``pawabase invoke --local`` and the test helpers all go through :func:`invoke`, so a function behaves the same in each: its input is validated against
``input_fields`` (defaults filled), it runs under its ``timeout``, a :class:`~pawabase.functions.FunctionError` becomes the status and code it asked for, anything
else becomes a 500 that names the exception, and everything it logged comes back with the answer.
"""

from __future__ import annotations

import asyncio
import time
import traceback
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .functions import FunctionContext, FunctionError, FunctionSpec, InputError, validate_input


@dataclass
class Invocation:
    """What came of one call."""

    function: str
    status: int = 200
    result: Any = None
    error: dict[str, Any] | None = None
    logs: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: float = 0.0
    traceback: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def body(self) -> dict[str, Any]:
        """The response body the platform would send: ``{"data": …}`` or ``{"error", "message", "details"?}``."""
        return {"data": self.result} if self.ok else dict(self.error or {})

    def raise_for_error(self) -> Any:
        """The result, or a :class:`FunctionError` carrying the failure (handy in tests)."""
        if self.error:
            raise FunctionError(self.error.get("message", ""), status=self.status, code=self.error.get("error", "error"), details=self.error.get("details"))
        return self.result


async def invoke(
    spec: FunctionSpec,
    input: Any = None,
    *,
    runtime: Any,
    auth: Mapping[str, Any] | None = None,
    project: str = "local",
    env: str = "development",
    branch: str = "main",
    trigger: str = "emulator",
    request: Mapping[str, Any] | None = None,
) -> Invocation:
    started = time.perf_counter()
    context = FunctionContext(input=input, auth=dict(auth or {}), project=project, env=env, runtime=runtime, trigger=trigger, request=request, branch=branch)
    out = Invocation(function=spec.name)
    try:
        context.input = validate_input(spec.input_fields, input)
        out.result = await asyncio.wait_for(spec.handler(context), timeout=spec.timeout)
    except TimeoutError:
        out.status, out.error = 504, {"error": "timeout", "message": f"function {spec.name!r} exceeded {spec.timeout}s"}
    except InputError as exc:
        out.status, out.error = 422, {"error": exc.code, "message": exc.message, "details": exc.details}
    except FunctionError as exc:
        out.status, out.error = exc.status, {"error": exc.code, "message": exc.message, **({"details": exc.details} if exc.details is not None else {})}
    except Exception as exc:  # noqa: BLE001 - a function's bug is an answer, with the traceback kept for the developer's console
        status, detail = getattr(exc, "status_code", None), getattr(exc, "detail", None)
        if isinstance(status, int) and 400 <= status < 600 and detail is not None:
            # An HTTP exception (Sillo's ``HTTPException``, or anything shaped like it) is the answer it describes, exactly as on the platform:
            # a dict detail is the response body, anything else is the message.
            out.status = status
            out.error = dict(detail) if isinstance(detail, dict) else {"error": "http_error", "message": str(detail)}
            out.logs = [*context.logs, *getattr(runtime, "logs", [])]
            out.duration_ms = round((time.perf_counter() - started) * 1000, 3)
            return out
        out.status, out.error = 500, {"error": "function_error", "message": f"{type(exc).__name__}: {exc}"}
        out.traceback = traceback.format_exc()
    out.logs = [*context.logs, *getattr(runtime, "logs", [])]
    out.duration_ms = round((time.perf_counter() - started) * 1000, 3)
    return out
