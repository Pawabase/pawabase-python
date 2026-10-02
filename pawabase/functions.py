"""Custom functions: the code you write, run by Pawabase or by ``pawabase emulate``.

A function is an ordinary ``async def`` that takes a :class:`FunctionContext`::

    from pawabase.functions import function

    @function("charge-order", description="Charge an order's card", policy="authenticated")
    async def charge_order(ctx):
        order = await ctx.runtime.resource_get("orders", ctx.input["order_id"])
        ...
        return {"charged": True}

The same file runs in three places and nothing in it changes:

* **on a Pawabase deployment**, after ``pawabase deploy`` uploaded it;
* **on your machine**, under ``pawabase emulate``, with ``ctx.runtime`` talking to a deployment over the network;
* **in a unit test**, with ``pawabase.testing.FakeRuntime`` standing in for the platform.

``ctx.runtime`` is the platform's capability set (resources, SQL, cache, events, flows, storage, mail, secrets, realtime). This module has no dependency on
any of it: it only describes functions, finds them in a project directory, and checks their input.
"""

from __future__ import annotations

import contextlib
import contextvars
import importlib.util
import inspect
import logging
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("pawabase.functions")

#: Functions are named like ``charge-order`` or ``stock.reserve``: a stable address in routes, flows, events and schedules.
NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
TRIGGERS = ("http", "flow", "job", "schedule", "event", "webhook", "emulator", "test")

#: What the platform's function policy language accepts for ``policy=`` (a name, or an inline condition). Checked on the platform, never here.
Policy = Any

_loading_project: contextvars.ContextVar[str] = contextvars.ContextVar("pawabase_loading_project", default="*")


class FunctionError(Exception):
    """Raise from a function to answer with a specific HTTP status and machine-readable code.

    ``raise FunctionError("That order is already paid.", status=409, code="already_paid")`` reaches the caller as
    ``{"error": "already_paid", "message": "..."}`` with status 409, instead of a 500.
    """

    def __init__(self, message: str, *, status: int = 400, code: str = "error", details: Any = None) -> None:
        super().__init__(message)
        self.message, self.status, self.code, self.details = message, status, code, details


class InputError(FunctionError):
    """The input did not match the function's ``input_fields``."""

    def __init__(self, problems: dict[str, str]) -> None:
        super().__init__("The input is not valid.", status=422, code="invalid", details=problems)
        self.problems = problems


@dataclass(slots=True)
class FunctionSpec:
    """A registered function.

    Attributes:
        name: How routes, flows, schedules, events and jobs refer to it.
        handler: ``async def handler(ctx) -> Any``.
        project: Which project registered it (``*`` for every project).
        description: Shown in Studio and ``pawabase functions``.
        policy: Who may invoke it over HTTP (``/functions/v1/<name>``).
        input_fields: Optional field definitions validating the input.
        timeout: Seconds before an invocation is cancelled.
        tags: Free labels, for listings.
    """

    name: str
    handler: Callable[..., Any]
    project: str = "*"
    description: str = ""
    policy: Policy = "authenticated"
    input_fields: list[dict[str, Any]] | None = None
    timeout: float = 30.0
    tags: tuple[str, ...] = ()

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "project": self.project,
            "description": self.description,
            "policy": self.policy,
            "input_fields": self.input_fields,
            "timeout": self.timeout,
            "tags": list(self.tags),
            "module": getattr(self.handler, "__module__", None),
        }


@dataclass(slots=True)
class FunctionContext:
    """What a function receives.

    Attributes:
        input: The invocation payload.
        auth: The caller's policy context (``authenticated``, ``user_id``, ``email``, ``roles``, ``permissions``).
        project, env: Where it runs. ``branch`` is the branch whose code is running (``main`` unless a feature branch was deployed).
        runtime: Platform capabilities (resources, SQL, cache, events, flows, storage, mail, secrets, realtime).
        trigger: ``http``, ``flow``, ``job``, ``schedule``, ``event``, ``webhook`` (``emulator`` and ``test`` under local tooling).
        request: For HTTP triggers, ``{"params", "query", "body", "headers", "client_ip"}``.
    """

    input: Any
    auth: Mapping[str, Any]
    project: str
    env: str
    runtime: Any
    trigger: str = "http"
    request: Mapping[str, Any] | None = None
    branch: str = "main"
    logs: list[dict[str, Any]] = field(default_factory=list)

    def log(self, message: str, **data: Any) -> None:
        """Add a line to the invocation's log (visible in ``pawabase logs`` and Studio)."""
        self.logs.append({"message": message, **data})


_registry: dict[tuple[str, str], FunctionSpec] = {}


def function(
    name: str,
    *,
    description: str = "",
    policy: Policy = "authenticated",
    input_fields: list[dict[str, Any]] | None = None,
    timeout: float = 30.0,
    tags: tuple[str, ...] | list[str] = (),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register an async function under *name* for the project being loaded."""
    if not NAME_PATTERN.match(name or ""):
        raise ValueError(f"{name!r} is not a valid function name (letters, digits and _ . : -, starting with a letter)")

    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        if not inspect.iscoroutinefunction(handler):
            raise TypeError(f"function {name!r} must be async")
        project = _loading_project.get()
        _registry[(project, name)] = FunctionSpec(
            name=name,
            handler=handler,
            project=project,
            description=description or (handler.__doc__ or "").strip().split("\n")[0],
            policy=policy,
            input_fields=input_fields,
            timeout=timeout,
            tags=tuple(tags),
        )
        return handler

    return decorator


def get_function(project: str, name: str) -> FunctionSpec | None:
    """The function *name* visible to *project* (its own first, then shared)."""
    return _registry.get((project, name)) or _registry.get(("*", name))


def get_exact(owner: str, name: str) -> FunctionSpec | None:
    """The function *name* registered by exactly *owner* (no fallback to shared code)."""
    return _registry.get((owner, name))


def list_functions(project: str) -> list[FunctionSpec]:
    """Every function visible to *project*."""
    own = {spec.name: spec for (owner, _), spec in _registry.items() if owner == project}
    shared = {spec.name: spec for (owner, _), spec in _registry.items() if owner == "*" and spec.name not in own}
    return sorted([*own.values(), *shared.values()], key=lambda spec: spec.name)


def clear_functions(project: str | None = None) -> None:
    """Forget registered functions, optionally only those owned by *project*.

    Deployments use this before loading a replacement artifact so removed source files cannot leave a stale callable in the process registry.
    """
    if project is None:
        _registry.clear()
        return
    for key in [key for key in _registry if key[0] == project]:
        del _registry[key]


# ── input validation ─────────────────────────────────────────────────────

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "text": (str,), "url": (str,), "email": (str,), "uuid": (str,), "date": (str,), "datetime": (str,),
    "integer": (int,), "number": (int, float), "boolean": (bool,), "array": (list,), "object": (dict,), "json": (object,),
}


def validate_input(fields: list[dict[str, Any]] | None, value: Any, *, partial: bool = False) -> Any:
    """Check *value* against ``input_fields`` and return it with ``default``\\s filled in. Raises :class:`InputError`.

    Understands ``type``, ``required``, ``default``, ``enum``, ``minimum``/``maximum`` (numbers), ``min_length``/``max_length`` (strings). Unknown fields pass
    through untouched: a function that wants a closed shape says so by listing every field. The platform validates with the same rules before it runs a
    function, so a function that passes here behaves the same there.
    """
    if not fields:
        return value
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise InputError({"": "The input must be an object."})
    out, problems = dict(value), {}
    for spec in fields:
        name = spec["name"]
        present = name in value and value[name] is not None
        if not present:
            if "default" in spec and not partial:
                out[name] = spec["default"]
            elif spec.get("required") and not partial:
                problems[name] = "This field is required."
            continue
        item = value[name]
        kind = spec.get("type", "string")
        allowed = _TYPES.get(kind, (object,))
        if kind in ("integer", "number") and isinstance(item, bool):
            problems[name] = f"Expected {kind}."
            continue
        if kind == "number" and isinstance(item, int):
            pass
        elif not isinstance(item, allowed):
            problems[name] = f"Expected {kind}."
            continue
        if spec.get("enum") and item not in spec["enum"]:
            problems[name] = "Choose one of: " + ", ".join(map(str, spec["enum"])) + "."
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            if "minimum" in spec and item < spec["minimum"]:
                problems[name] = f"Must be at least {spec['minimum']}."
            if "maximum" in spec and item > spec["maximum"]:
                problems[name] = f"Must be at most {spec['maximum']}."
        if isinstance(item, str):
            if "min_length" in spec and len(item) < spec["min_length"]:
                problems[name] = f"Must be at least {spec['min_length']} characters."
            if "max_length" in spec and len(item) > spec["max_length"]:
                problems[name] = f"Must be at most {spec['max_length']} characters."
    if problems:
        raise InputError(problems)
    return out


# ── loading a project's code ─────────────────────────────────────────────


@dataclass(slots=True)
class ProjectCode:
    """What loading one project's code directory produced."""

    project: str
    modules: list[str] = field(default_factory=list)
    router: Any = None
    errors: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)


def _import_file(module_name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def module_prefix(key: str) -> str:
    return "pawabase_code." + re.sub(r"[^A-Za-z0-9_]", "_", key)


@contextlib.contextmanager
def import_scope(root: Path, purge_under: Path | tuple[Path, ...] | list[Path] | None = None, extra_paths: tuple[Path, ...] | list[Path] = ()):
    """Make ``import helper_package`` inside the code being loaded find the helpers shipped next to it.

    Puts *root* first on ``sys.path`` for the duration, and first forgets every module that an *earlier* artifact imported from under *purge_under*, so two
    deployments that each ship their own ``helpers`` package never see each other's. Functions that are already loaded keep their own references; only a
    *later* import by name could miss, which is why code that is deployed imports everything at the top of the file.
    """
    saved = list(sys.path)
    if purge_under is not None:
        roots = [purge_under] if isinstance(purge_under, Path) else list(purge_under)
        prefixes = tuple(str(p.resolve()) + "/" for p in roots)
        for name, module in list(sys.modules.items()):
            origin = getattr(module, "__file__", None)
            if origin and str(Path(origin).resolve()).startswith(prefixes) and not name.startswith("pawabase_code."):
                del sys.modules[name]
    for extra in reversed([*extra_paths, root]):
        sys.path.insert(0, str(extra))
    try:
        yield
    finally:
        sys.path[:] = saved


def load_code_dir(
    base: str | Path,
    key: str,
    *,
    only_functions: bool = False,
    purge_under: Path | tuple[Path, ...] | list[Path] | None = None,
    extra_paths: tuple[Path, ...] | list[Path] = (),
) -> ProjectCode:
    """Import the code in *base* and register its functions under *key*.

    ``<base>/functions/*.py`` always; ``policies.py``, ``transformers.py`` and ``routes.py`` too unless *only_functions*. Import errors are collected and
    reported, not raised: one broken module must not stop the platform from serving every other project, and ``pawabase deploy`` shows them to you before
    anything is uploaded.
    """
    result = ProjectCode(project=key)
    base = Path(base)
    if not base.is_dir():
        return result
    token = _loading_project.set(key)
    try:
        files = sorted((base / "functions").glob("*.py")) if (base / "functions").is_dir() else []
        if not only_functions:
            files += [base / name for name in ("policies.py", "transformers.py", "routes.py") if (base / name).is_file()]
        with import_scope(base, purge_under, extra_paths):
            for path in files:
                if path.name.startswith("_"):
                    continue
                module_name = f"{module_prefix(key)}.{path.parent.name}.{path.stem}" if path.parent != base else f"{module_prefix(key)}.{path.stem}"
                try:
                    module = _import_file(module_name, path)
                    result.modules.append(module_name)
                    if path.name == "routes.py":
                        result.router = getattr(module, "router", None)
                except Exception as exc:  # noqa: BLE001 - reported, never raised
                    logger.exception("could not load %s", path)
                    result.errors.append(f"{path.relative_to(base)}: {type(exc).__name__}: {exc}")
    finally:
        _loading_project.reset(token)
    result.functions = sorted(spec.name for (owner, _), spec in _registry.items() if owner == key)
    return result


def load_project_code(root: str | Path, project: str) -> ProjectCode:
    """Import ``<root>/<project>``: the project's own code directory, mounted on the platform."""
    return load_code_dir(Path(root) / project, project)


def load_functions(directory: str | Path, *, project: str = "local", paths: tuple[str | Path, ...] | list[str | Path] = (), reload: bool = False) -> ProjectCode:
    """Import the functions found in *directory* for local tooling (``pawabase emulate``, ``deploy --check``, tests).

    *directory* is a folder of ``*.py`` files named ``functions`` (its parent is then the code root), or a code root that has a ``functions/`` folder.
    The code root (and any *paths*) is put on ``sys.path`` while loading, so functions can import helper packages that live next to them, as they will once deployed.
    """
    base = Path(directory).resolve()
    root = base.parent if base.name == "functions" else base
    clear_functions(project)
    extra = [Path(p).resolve() for p in paths]
    # ``reload`` forgets every helper module that was imported from the project's own files, so an edited helper is read again (the emulator's watch mode).
    return load_code_dir(root, project, only_functions=True, purge_under=[root, *extra] if reload else None, extra_paths=extra)


__all__ = [
    "FunctionContext",
    "FunctionError",
    "FunctionSpec",
    "InputError",
    "ProjectCode",
    "clear_functions",
    "function",
    "get_exact",
    "get_function",
    "list_functions",
    "import_scope",
    "load_code_dir",
    "load_functions",
    "module_prefix",
    "load_project_code",
    "validate_input",
]
