"""``pawabase emulate``: your functions, running on your machine, wired to a real deployment.

Run it from the project, point your app (or ``curl``) at ``http://127.0.0.1:8787`` instead of the gateway, and it behaves like the gateway with one difference:
**the functions you have locally run locally.**

* ``POST /functions/v1/<name>`` runs your local copy of the function when you have one; otherwise it is forwarded to the deployment.
* ``/rest/v1/<path>`` that the deployment's routes bind to a *function you have locally* runs it here, with the payload the platform builds
  (``params``, ``query``, ``body``, ``headers``, ``client_ip``) and the route's policy checked by the deployment.
* **Everything else is forwarded to the deployment unchanged**: the data API, auth, storage, flows, realtime. Your app signs in against the real Akountz, reads the real
  data, and calls your local code for the parts you are working on.
* Inside a local function, ``ctx.runtime`` is a :class:`~pawabase.runtime.RemoteRuntime`: queries, events, flows, cache, storage and secrets act on the deployment
  (as the caller), outbound HTTP leaves from your machine, and ``call_function`` prefers your local functions.
* **Triggers.** :meth:`Emulator.trigger_event`, :meth:`~Emulator.trigger_schedule` and :meth:`~Emulator.trigger_flow` (the ``pawabase trigger`` command, or
  ``POST /_emulator/trigger/…``) run what is subscribed to an event or a schedule: local functions locally, everything else on the deployment.
* ``--watch`` reloads your code when a file changes.

The deployment does **not** call back into your machine: a real ``order.paid`` happening there runs the *deployed* subscribers. Use a trigger to run yours.

Control endpoints live under ``/_emulator``: ``health``, ``functions``, ``routes``, ``runs``, ``refresh``, ``trigger/event/<name>``, ``trigger/schedule/<name>``,
``trigger/flow/<name>``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from .client import AsyncPawabase, PawabaseError
from .config import Settings
from .functions import FunctionSpec, ProjectCode, get_exact, list_functions, load_functions
from .invoke import Invocation, invoke
from .runtime import RemoteRuntime

HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length", "content-encoding"})
LOCAL_PROJECT = "emulated"
MAX_BODY = 8 * 1024 * 1024


@dataclass
class LocalRoute:
    """A route of the deployment bound to a function that exists locally."""

    method: str
    path: str
    handler: str
    policy: Any
    input_fields: list[dict[str, Any]] | None
    pattern: re.Pattern[str]
    names: list[str]

    @classmethod
    def from_definition(cls, route: Mapping[str, Any]) -> LocalRoute:
        names: list[str] = []

        def part(match: re.Match[str]) -> str:
            names.append(match.group(1))
            return f"(?P<{match.group(1)}>[^/]+)"

        template = route["path"]
        regex = "^" + re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[A-Za-z]+)?\}", part, re.escape(template).replace(r"\{", "{").replace(r"\}", "}")) + "$"
        return cls(route["method"].upper(), template, route["handler"], route.get("policy", "authenticated"), route.get("input_fields"), re.compile(regex), names)

    def match(self, method: str, path: str) -> dict[str, str] | None:
        if method.upper() != self.method:
            return None
        found = self.pattern.match(path.rstrip("/") or "/")
        return found.groupdict() if found else None


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str]

    @classmethod
    def json(cls, status: int, value: Any, headers: Mapping[str, str] | None = None) -> Response:
        return cls(status, json.dumps(value, default=str).encode(), {"content-type": "application/json", **(headers or {})})


class Emulator:
    """The local gateway. Create it, ``await start()``, then either ``serve()`` or call :meth:`handle` yourself (the tests do)."""

    def __init__(
        self,
        settings: Settings,
        *,
        host: str = "127.0.0.1",
        port: int = 8787,
        watch: bool = False,
        client: AsyncPawabase | None = None,
        log: Callable[[str], None] | None = print,
    ) -> None:
        self.settings = settings.require("url", "api_key")
        self.host, self.port, self.watch = host, port, watch
        self.client = client or AsyncPawabase(settings.url or "", settings.api_key or "", environment=settings.environment)
        self.log = log or (lambda _line: None)
        self.runtime = RemoteRuntime(self.client, branch=settings.branch if settings.branch != "main" else None)
        self.code: ProjectCode | None = None
        self.routes: list[LocalRoute] = []
        self.subscriptions: list[dict[str, Any]] = []
        self.schedules: dict[str, dict[str, Any]] = {}
        self.runs: deque[dict[str, Any]] = deque(maxlen=200)
        self.proxy = httpx.AsyncClient(base_url=self.settings.url or "", timeout=60.0, follow_redirects=False)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: ThreadingHTTPServer | None = None
        self._stamp = 0.0
        self.local_functions: dict[str, Callable[[Any], Any]] = {}

    # ── loading ─────────────────────────────────────────────────────────

    @property
    def project_key(self) -> str:
        return LOCAL_PROJECT

    def load_code(self, *, reload: bool = False) -> ProjectCode:
        paths: list[str | Path] = [self.settings.root / p for p in self.settings.paths]
        self.code = load_functions(self.settings.functions_dir, project=self.project_key, paths=paths, reload=reload)
        self.local_functions = {spec.name: self._local_caller(spec) for spec in list_functions(self.project_key) if spec.project == self.project_key}
        self.runtime.local_functions = self.local_functions
        return self.code

    def _local_caller(self, spec: FunctionSpec) -> Callable[[Any], Any]:
        async def call(input: Any) -> Any:
            outcome = await invoke(spec, input, runtime=self.runtime, project=LOCAL_PROJECT, env=self.settings.environment, branch=self.settings.branch, trigger="flow")
            return outcome.raise_for_error()

        return call

    async def refresh(self) -> dict[str, Any]:
        """Re-read the deployment's routes, subscriptions and schedules (what to run locally, and what to leave to the deployment)."""
        branch = self.settings.branch if self.settings.branch != "main" else None
        routes = (await self.client.definitions("routes", branch=branch)).get("data", [])
        self.subscriptions = [s for s in (await self.client.definitions("subscriptions", branch=branch)).get("data", []) if s.get("enabled", True)]
        self.schedules = {s["name"]: s for s in (await self.client.definitions("schedules", branch=branch)).get("data", [])}
        self.routes = [LocalRoute.from_definition(r) for r in routes if r.get("handler_type") == "function" and r.get("enabled", True) and r["handler"] in self.local_functions]
        return {"routes": len(self.routes), "subscriptions": len(self.subscriptions), "schedules": len(self.schedules)}

    async def start(self) -> dict[str, Any]:
        """Load local code and learn the deployment's definitions. Raises :class:`PawabaseError` if the deployment cannot be reached or the key is wrong."""
        code = self.load_code()
        deployed = await self.client.functions()
        counts = await self.refresh()
        return {
            "local": code.functions, "errors": code.errors, "deployed": [f["name"] for f in deployed.get("data", [])],
            "routes_served_locally": counts["routes"], "subscriptions": counts["subscriptions"], "schedules": counts["schedules"],
        }

    async def aclose(self) -> None:
        await self.proxy.aclose()
        await self.runtime.aclose()
        await self.client.close()

    # ── who is calling ──────────────────────────────────────────────────

    async def caller(self, headers: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
        """``(auth, credential)`` for a request: the user the token proves (verified by the deployment), and what the API key is."""
        auth = {"authenticated": False, "kind": "anonymous", "user_id": None, "email": None, "roles": [], "permissions": []}
        bearer = headers.get("authorization", "")
        key = headers.get("apikey", "")
        if bearer.lower().startswith("bearer ") and not bearer[7:].startswith("pb_sk_") and not bearer[7:].startswith("pb_pk_"):
            try:
                auth = (await self.client.identify(bearer[7:].strip())).get("auth", auth)
            except PawabaseError:
                pass
        secret = key.startswith("pb_sk_") or bearer[7:].startswith("pb_sk_")
        return auth, {"is_service": secret, "role": "service" if secret else "anon", "scopes": []}

    async def _allowed(self, policy: Any, auth: dict[str, Any], credential: dict[str, Any], payload: Mapping[str, Any]) -> Response | None:
        """``None`` when the policy allows the call, else the 401/403 to answer with. A secret key bypasses policies, as on the platform."""
        if credential["is_service"]:
            return None
        context = {"auth": auth, "credential": credential, "request": {"method": payload.get("method"), "path": payload.get("path"), "ip": payload.get("client_ip")},
                   "env": self.settings.environment, "input": payload.get("body"), "record": None}
        try:
            allowed = await self.client.runtime_call("check_policy", [policy, context])
        except PawabaseError as error:
            return Response.json(502, {"error": "policy_unavailable", "message": f"The deployment could not evaluate the policy: {error.message}"})
        if allowed.get("result"):
            return None
        if not auth.get("authenticated"):
            return Response.json(401, {"error": "unauthenticated", "message": "Authentication required"})
        return Response.json(403, {"error": "forbidden", "message": "Your account may not do that."})

    # ── serving a request ───────────────────────────────────────────────

    async def handle(self, method: str, target: str, headers: Mapping[str, str], body: bytes, client_ip: str | None = None) -> Response:
        """Answer one request: control endpoint, local function, local route, or forward."""
        started = time.perf_counter()
        parts = urlsplit(target)
        path, query = parts.path or "/", dict(parse_qsl(parts.query, keep_blank_values=True))
        lowered = {k.lower(): v for k, v in headers.items()}
        try:
            if lowered.get("upgrade", "").lower() == "websocket":
                # A plain HTTP server cannot relay a WebSocket. Realtime does not involve your functions, so connect to the deployment directly.
                response = Response.json(501, {"error": "websocket_not_proxied", "message": f"The emulator does not proxy WebSockets. Connect realtime to {self.settings.url} directly."})
            elif path.startswith("/_emulator"):
                response = await self._control(method, path[len("/_emulator"):] or "/", query, body)
            elif method == "POST" and path.startswith("/functions/v1/") and path.rsplit("/", 1)[-1] in self.local_functions:
                response = await self._function(path.rsplit("/", 1)[-1], query, lowered, body, client_ip)
            else:
                route = self._route_for(method, path)
                response = await self._route(route[0], route[1], path, query, lowered, body, client_ip) if route else await self.forward(method, target, lowered, body, client_ip)
        except Exception as error:  # noqa: BLE001 - the emulator must stay up whatever a request does
            response = Response.json(500, {"error": "emulator_error", "message": f"{type(error).__name__}: {error}"})
        self.log(f"{method:6} {path}  {response.status}  {round((time.perf_counter() - started) * 1000)}ms" + ("  (forwarded)" if response.headers.get("x-emulator") == "forwarded" else ""))
        return response

    def _route_for(self, method: str, path: str) -> tuple[LocalRoute, dict[str, str]] | None:
        if not path.startswith("/rest/v1"):
            return None
        inner = path[len("/rest/v1"):] or "/"
        for route in self.routes:
            found = route.match(method, inner)
            if found is not None:
                return route, found
        return None

    async def _run(self, name: str, input: Any, auth: dict[str, Any], *, trigger: str, request: Mapping[str, Any] | None, branch_override: str | None = None) -> Invocation:
        spec = get_exact(self.project_key, name)
        if spec is None:
            raise LookupError(name)
        runtime = self.runtime.for_caller(auth, branch=branch_override)
        outcome = await invoke(spec, input, runtime=runtime, auth=auth, project=LOCAL_PROJECT, env=self.settings.environment, branch=self.settings.branch, trigger=trigger, request=request)
        self.runs.appendleft({"function": name, "status": outcome.status, "ok": outcome.ok, "duration_ms": outcome.duration_ms, "trigger": trigger, "at": time.time(),
                              "error": (outcome.error or {}).get("message"), "logs": outcome.logs[-20:]})
        if outcome.traceback:
            self.log(outcome.traceback.rstrip())
        return outcome

    async def _function(self, name: str, query: Mapping[str, str], headers: Mapping[str, str], body: bytes, client_ip: str | None) -> Response:
        spec = get_exact(self.project_key, name)
        assert spec is not None
        payload, input_ = self._body(body)
        if payload is not None:
            return payload
        auth, credential = await self.caller(headers)
        denied = await self._allowed(spec.policy, auth, credential, {"method": "POST", "path": f"/functions/v1/{name}", "client_ip": client_ip, "body": input_})
        if denied:
            return denied
        outcome = await self._run(name, input_, auth, trigger="http", request=None)
        return Response.json(outcome.status, outcome.body)

    async def _route(self, route: LocalRoute, params: dict[str, str], path: str, query: Mapping[str, str], headers: Mapping[str, str], body: bytes, client_ip: str | None) -> Response:
        bad, parsed = self._body(body)
        if bad is not None:
            return bad
        auth, credential = await self.caller(headers)
        request = {"params": params, "query": dict(query), "body": parsed, "headers": {k: v for k, v in headers.items() if k not in ("apikey", "x-api-key", "cookie", "proxy-authorization") and not k.startswith("x-pawabase-")}, "client_ip": client_ip}
        denied = await self._allowed(route.policy, auth, credential, {"method": route.method, "path": path, "client_ip": client_ip, "body": parsed})
        if denied:
            return denied
        input_ = {**params, **parsed} if isinstance(parsed, dict) else (parsed if parsed is not None else dict(params))
        outcome = await self._run(route.handler, input_, auth, trigger="http", request=request)
        # A route answers with the function's result itself; /functions/v1 wraps it in {"data": …}.
        return Response.json(outcome.status, outcome.result if outcome.ok else outcome.body)

    @staticmethod
    def _body(body: bytes) -> tuple[Response | None, Any]:
        if len(body) > MAX_BODY:
            return Response.json(413, {"error": "too_large", "message": "The body is too large."}), None
        if not body.strip():
            return None, None
        try:
            return None, json.loads(body)
        except ValueError:
            return Response.json(400, {"error": "bad_request", "message": "The body must be JSON."}), None

    async def forward(self, method: str, target: str, headers: Mapping[str, str], body: bytes, client_ip: str | None) -> Response:
        """Send a request to the deployment exactly as received, and relay the answer."""
        outgoing = {k: v for k, v in headers.items() if k not in HOP_BY_HOP}
        if client_ip:
            outgoing["x-forwarded-for"] = f"{headers['x-forwarded-for']}, {client_ip}" if "x-forwarded-for" in headers else client_ip
        try:
            upstream = await self.proxy.request(method, target, headers=outgoing, content=body or None)
        except httpx.TransportError as error:
            return Response.json(502, {"error": "deployment_unreachable", "message": f"Could not reach {self.settings.url}: {error}"})
        relayed = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP}
        return Response(upstream.status_code, upstream.content, {**relayed, "x-emulator": "forwarded"})

    # ── control endpoints ───────────────────────────────────────────────

    async def _control(self, method: str, path: str, query: Mapping[str, str], body: bytes) -> Response:
        if path == "/health":
            return Response.json(200, {"status": "ok", "environment": self.settings.environment, "branch": self.settings.branch})
        if path == "/functions":
            return Response.json(200, {"data": [{**spec.describe(), "source": "local"} for spec in list_functions(self.project_key) if spec.project == self.project_key], "errors": self.code.errors if self.code else []})
        if path == "/routes":
            return Response.json(200, {"data": [{"method": r.method, "path": f"/rest/v1{r.path}", "function": r.handler, "policy": r.policy} for r in self.routes]})
        if path == "/runs":
            return Response.json(200, {"data": list(self.runs)})
        if path == "/refresh" and method == "POST":
            self.load_code(reload=True)
            return Response.json(200, {**await self.refresh(), "errors": self.code.errors if self.code else []})
        trigger = re.match(r"^/trigger/(event|schedule|flow)/(.+)$", path)
        if trigger and method == "POST":
            _, parsed = self._body(body)
            kind, name = trigger.group(1), trigger.group(2)
            if kind == "event":
                return Response.json(200, await self.trigger_event(name, parsed, remote_only=query.get("remote") == "1"))
            if kind == "schedule":
                return Response.json(200, await self.trigger_schedule(name))
            return Response.json(200, await self.trigger_flow(name, parsed))
        return Response.json(404, {"error": "not_found", "message": f"No emulator endpoint {path}"})

    # ── triggers ────────────────────────────────────────────────────────

    async def trigger_event(self, name: str, payload: Any = None, *, remote_only: bool = False) -> dict[str, Any]:
        """Publish *name*. With ``remote_only`` it is simply emitted on the deployment. Otherwise each subscription that matches runs where its target lives:
        a function you have locally runs here, a flow or a deployed-only function runs on the deployment, and the event is *not* emitted (it would run the deployed
        copy of your local function a second time)."""
        if remote_only:
            return {"mode": "remote", "event_id": (await self.client.emit_event(name, payload)).get("event_id")}
        event = {"name": name, "payload": payload, "source": "emulator", "env": self.settings.environment, "occurred_at": time.time()}
        results = []
        for sub in self.subscriptions:
            if not fnmatch.fnmatchcase(name, sub["event"]):
                continue
            target, kind = sub["target"], sub.get("target_type", "function")
            if kind == "function" and target in self.local_functions:
                outcome = await self._run(target, {"event": event}, {"authenticated": False, "kind": "system"}, trigger="event", request=None)
                results.append({"subscription": sub["name"], "ran": "local", "function": target, "status": outcome.status, "result": outcome.result, "error": outcome.error})
            elif kind == "function":
                job = await self.client.run_function(target, {"event": event}, branch=self.settings.branch if self.settings.branch != "main" else None)
                results.append({"subscription": sub["name"], "ran": "deployment", "function": target, **job})
            elif kind == "flow":
                results.append({"subscription": sub["name"], "ran": "deployment", "flow": target, **(await self.client.run_flow(target, {"event": event}))})
            else:
                results.append({"subscription": sub["name"], "ran": "skipped", "reason": f"a {kind} subscription is only run by the deployment"})
        return {"mode": "local", "event": name, "matched": len(results), "results": results}

    async def trigger_schedule(self, name: str) -> dict[str, Any]:
        """Fire a schedule now. A function target you have locally runs here; anything else is fired on the deployment, as the scheduler would."""
        schedule = self.schedules.get(name)
        if schedule is None:
            raise LookupError(f"no schedule {name!r}")
        target = schedule.get("target")
        if schedule.get("target_type") == "function" and target in self.local_functions:
            outcome = await self._run(target, schedule.get("payload") or {}, {"authenticated": False, "kind": "system"}, trigger="schedule", request=None)
            return {"ran": "local", "function": target, "status": outcome.status, "result": outcome.result, "error": outcome.error}
        return {"ran": "deployment", **(await self.client.run_schedule(name))}

    async def trigger_flow(self, name: str, input: Any = None) -> dict[str, Any]:
        """Run a flow on the deployment (flows live there)."""
        return {"ran": "deployment", **(await self.client.run_flow(name, input))}

    # ── serving ─────────────────────────────────────────────────────────

    def _changed(self) -> bool:
        newest = 0.0
        roots = [self.settings.functions_dir, *[(self.settings.root / p) for p in (*self.settings.include, *self.settings.paths)]]
        for root in roots:
            for path in (root.rglob("*.py") if root.is_dir() else [root]):
                if "__pycache__" in path.parts:
                    continue
                try:
                    newest = max(newest, path.stat().st_mtime)
                except OSError:
                    continue
        changed = self._stamp and newest > self._stamp
        self._stamp = newest
        return bool(changed)

    async def _watch(self) -> None:
        self._changed()
        while True:
            await asyncio.sleep(0.7)
            if self._changed():
                self.load_code(reload=True)
                counts = await self.refresh()
                problems = self.code.errors if self.code else []
                self.log(f"reloaded: {len(self.local_functions)} functions, {counts['routes']} routes served locally" + (f"; ERRORS: {problems}" if problems else ""))

    def serve(self, on_ready: Callable[[dict[str, Any]], None] | None = None) -> None:
        """Run until interrupted. Blocks. ``on_ready`` receives what :meth:`start` learned, once the server is accepting requests."""
        emulator = self
        loop = asyncio.new_event_loop()
        self._loop = loop
        thread = threading.Thread(target=loop.run_forever, daemon=True, name="pawabase-emulator-loop")
        thread.start()
        # Everything that talks to the deployment lives on this loop, so it starts here and not on the caller's.
        try:
            info = asyncio.run_coroutine_threadsafe(self.start(), loop).result()
        except BaseException:
            loop.call_soon_threadsafe(loop.stop)
            raise

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                return

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {k: v for k, v in self.headers.items()}
                future = asyncio.run_coroutine_threadsafe(emulator.handle(self.command, self.path, headers, body, self.client_address[0]), loop)
                response = future.result()
                self.send_response(response.status)
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(response.body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(response.body)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _serve

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        if self.watch:
            asyncio.run_coroutine_threadsafe(self._watch(), loop)
        if on_ready:
            on_ready(info)
        try:
            self._server.serve_forever()
        finally:
            self._server.server_close()
            loop.call_soon_threadsafe(loop.stop)

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()
