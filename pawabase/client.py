"""The public Pawabase API client, used by applications, the CLI and the emulator.

``Pawabase`` (blocking) and ``AsyncPawabase`` share every method: the endpoint methods are written once, against ``self._call``, which a blocking client
answers with a value and an async client with an awaitable.

Authentication is the project's key in the ``apikey`` header. A *publishable* key (``pb_pk_…``) is what a browser or app holds: it reaches the data plane
under the environment's policies. A *secret* key (``pb_sk_…``) is what a server, the CLI and the emulator hold: it also reaches the management plane for **its own
environment** (deploying functions, reading runs, firing events). The gateway turns the key into a signed environment context; the raw key is never
forwarded to a function or to an internal service.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

RETRIED_STATUSES = frozenset({502, 503, 504})
#: A 429 means the request was *not* processed, so it is safe to wait and send it again whatever the method. The wait is what the gateway asked for (capped), because
#: ``pawabase emulate`` is chatty (every database statement is a request) and a rate limit should slow it down, not break it.
MAX_RATE_WAITS = 8
MAX_RATE_WAIT_SECONDS = 20.0


def rate_wait(response: httpx.Response) -> float:
    """How long the gateway asked to wait (``Retry-After`` or the body's ``retry_after``), at least one second, at most :data:`MAX_RATE_WAIT_SECONDS`."""
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


class PawabaseError(RuntimeError):
    """A Pawabase API request failed.

    Attributes:
        status_code: The HTTP status (``0`` when the gateway could not be reached at all).
        detail: The body the API answered with.
        code, message: The machine-readable code and the sentence, when the body had them.
    """

    def __init__(self, status_code: int, detail: Any) -> None:
        self.status_code, self.detail = status_code, detail
        self.code = detail.get("error") if isinstance(detail, dict) and isinstance(detail.get("error"), str) else None
        self.message = _message(detail)
        super().__init__(f"Pawabase API returned {status_code}: {self.message}")

    @property
    def problems(self) -> list[str]:
        """The per-item problems a refused deployment lists (``problems`` in the body), if any."""
        body = self.detail if isinstance(self.detail, dict) else {}
        for holder in (body, body.get("detail") if isinstance(body.get("detail"), dict) else {}):
            if isinstance(holder.get("problems"), list):
                return [str(p) for p in holder["problems"]]
        return []


def _message(detail: Any) -> str:
    if isinstance(detail, dict):
        inner = detail.get("detail", detail.get("message", detail.get("error")))
        if isinstance(inner, dict):
            return str(inner.get("message") or inner)
        if isinstance(inner, list):
            return "; ".join(str(i.get("msg", i)) if isinstance(i, dict) else str(i) for i in inner)
        return str(inner) if inner is not None else str(detail)
    return str(detail)


class _Endpoints:
    """Every endpoint, once. ``_call`` is supplied by the concrete client."""

    url: str
    environment: str

    def _call(self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None, retry: bool = False) -> Any:
        raise NotImplementedError

    # ── identity (the signed-in user's own session) ─────────────────────

    def sign_in(self, email: str, password: str) -> Any:
        """``POST /auth/v1/token``: a session (``access_token``, ``refresh_token``, ``user``), or ``{"mfa_required": true, "mfa_token", "factors"}``."""
        return self._call("POST", "/auth/v1/token", json={"grant_type": "password", "email": email, "password": password})

    def sign_up(self, email: str, password: str, *, name: str | None = None, data: Mapping[str, Any] | None = None) -> Any:
        body: dict[str, Any] = {"email": email, "password": password}
        if name:
            body["name"] = name
        if data:
            body["data"] = dict(data)
        return self._call("POST", "/auth/v1/signup", json=body)

    def refresh_session(self, refresh_token: str) -> Any:
        return self._call("POST", "/auth/v1/token", json={"grant_type": "refresh_token", "refresh_token": refresh_token})

    def sign_out(self) -> Any:
        return self._call("POST", "/auth/v1/logout", json={"scope": "local"})

    def request_password_reset(self, email: str, *, redirect_to: str | None = None) -> Any:
        return self._call("POST", "/auth/v1/recover", json={"email": email, **({"redirect_to": redirect_to} if redirect_to else {})})

    def reset_password(self, token: str, password: str) -> Any:
        return self._call("POST", "/auth/v1/recover/confirm", json={"token": token, "password": password})

    def auth_settings(self) -> Any:
        return self._call("GET", "/auth/v1/settings", retry=True)

    def _manage(self, suffix: str) -> str:
        return f"/platform/v1/envs/{quote(self.environment)}{suffix}"

    # ── data plane ──────────────────────────────────────────────────────

    def request(self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None) -> Any:
        """Any request, as the key's holder. ``path`` is relative to the gateway (``/rest/v1/posts``)."""
        return self._call(method, path if path.startswith("/") else "/" + path, json=json, params=params, retry=method.upper() in ("GET", "HEAD"))

    def list(self, resource: str, **params: Any) -> Any:
        return self.request("GET", f"/rest/v1/{resource}", params=params)

    def get(self, resource: str, record_id: str) -> Any:
        return self.request("GET", f"/rest/v1/{resource}/{record_id}")

    def create(self, resource: str, values: Mapping[str, Any]) -> Any:
        return self.request("POST", f"/rest/v1/{resource}", json=dict(values))

    def update(self, resource: str, record_id: str, values: Mapping[str, Any]) -> Any:
        return self.request("PATCH", f"/rest/v1/{resource}/{record_id}", json=dict(values))

    def delete(self, resource: str, record_id: str) -> Any:
        return self.request("DELETE", f"/rest/v1/{resource}/{record_id}")

    def invoke_function(self, name: str, input: Any = None, *, branch: str | None = None) -> Any:
        """``POST /functions/v1/<name>`` under the function's own policy (the key's ``functions:invoke`` scope applies)."""
        return self._call("POST", f"/functions/v1/{quote(name)}", json=input, params={"branch": branch} if branch and branch != "main" else None)

    def invoke_flow(self, name: str, input: Any = None) -> Any:
        return self._call("POST", f"/flows/v1/{quote(name)}", json=input)

    # ── functions: deploying and running them ───────────────────────────

    def functions(self, *, branch: str | None = None) -> Any:
        """Functions visible to the environment (and *branch*), each saying whether it came from a branch, a deployment or the runtime's mounted code."""
        return self._call("GET", self._manage("/functions"), params={"branch": branch} if branch else None, retry=True)

    def deploy_functions(
        self, archive: str, *, branch: str = "main", manifest: Mapping[str, Any] | None = None, runtime: str = "python3.11", limits: Mapping[str, Any] | None = None
    ) -> Any:
        """Upload a base64 ``.tar.gz`` bundle and activate it. Not retried: a half-seen deploy must be looked at, not repeated."""
        return self._call("POST", self._manage("/function-deployments"), json={"archive": archive, "branch": branch, "manifest": dict(manifest or {}), "runtime": runtime, "limits": dict(limits or {})})

    def deployments(self, *, branch: str | None = None, limit: int = 50) -> Any:
        params: dict[str, Any] = {"limit": limit}
        if branch:
            params["branch"] = branch
        return self._call("GET", self._manage("/function-deployments"), params=params, retry=True)

    def deployment(self, deployment_id: str) -> Any:
        return self._call("GET", self._manage(f"/function-deployments/{quote(deployment_id)}"), retry=True)

    def activate_deployment(self, deployment_id: str) -> Any:
        """Make an earlier deployment the active one again (a rollback)."""
        return self._call("POST", self._manage(f"/function-deployments/{quote(deployment_id)}/activate"))

    def remove_deployment(self, deployment_id: str) -> Any:
        return self._call("DELETE", self._manage(f"/function-deployments/{quote(deployment_id)}"))

    def function_branches(self) -> Any:
        return self._call("GET", self._manage("/function-branches"), retry=True)

    def remove_function_branch(self, branch: str) -> Any:
        """Delete the functions deployed on *branch*. The environment's own are untouched."""
        return self._call("DELETE", self._manage(f"/function-branches/{quote(branch)}"))

    def function_runs(self, function: str | None = None, *, branch: str | None = None, status: str | None = None, after: str | None = None, limit: int = 50) -> Any:
        params = {k: v for k, v in {"function": function, "branch": branch, "status": status, "after": after, "limit": limit}.items() if v not in (None, "")}
        return self._call("GET", self._manage("/function-runs"), params=params, retry=True)

    function_logs = function_runs

    def function_run(self, run_id: str) -> Any:
        return self._call("GET", self._manage(f"/function-runs/{quote(run_id)}"), retry=True)

    def run_function(self, name: str, input: Any = None, *, branch: str | None = None, as_user: Mapping[str, Any] | None = None) -> Any:
        """Run a deployed function now as an operator (or ``as_user``), bypassing its HTTP policy. Returns ``{"status", "result" | "error"}``."""
        return self._call("POST", self._manage(f"/functions/{quote(name)}/invoke"), json={"input": input, "branch": branch, "as_user": dict(as_user) if as_user else None})

    # ── triggers: events, flows, schedules ──────────────────────────────

    def emit_event(self, name: str, payload: Any = None) -> Any:
        """Publish an event on the environment: every subscription and flow listening for it runs."""
        return self._call("POST", self._manage("/events"), json={"name": name, "payload": payload})

    def events(self, name: str | None = None, *, limit: int = 20) -> Any:
        params: dict[str, Any] = {"limit": limit}
        if name:
            params["name"] = name
        return self._call("GET", self._manage("/events"), params=params, retry=True)

    def run_flow(self, name: str, input: Any = None, *, as_user: Mapping[str, Any] | None = None, entry: str | None = None) -> Any:
        return self._call("POST", self._manage(f"/flows/{quote(name)}/run"), json={"input": input, "as_user": dict(as_user) if as_user else None, "entry": entry})

    def run_schedule(self, name: str) -> Any:
        """Fire a schedule now, exactly as the scheduler would."""
        return self._call("POST", self._manage(f"/schedules/{quote(name)}/run"))

    def definitions(self, kind: str, *, branch: str | None = None) -> Any:
        """The environment's ``routes``, ``flows``, ``schedules``, ``subscriptions``, ``inbound-hooks`` … (a branch's draft, when *branch* is given)."""
        return self._call("GET", self._manage(f"/{kind}"), params={"branch": branch} if branch and branch != "main" else None, retry=True)

    def branches(self) -> Any:
        return self._call("GET", self._manage("/branches"), retry=True)

    # ── the runtime, remotely ───────────────────────────────────────────

    def runtime_call(self, method: str, args: list[Any] | None = None, kwargs: Mapping[str, Any] | None = None, *, as_user: Mapping[str, Any] | None = None, branch: str | None = None) -> Any:
        """One ``ctx.runtime`` method, executed on the deployment (needs the ``runtime:use`` scope). Values are in :mod:`pawabase.codec` form."""
        return self._call("POST", self._manage("/runtime/call"), json={"method": method, "args": args or [], "kwargs": dict(kwargs or {}), "as_user": dict(as_user) if as_user else None, "branch": branch})

    def identify(self, token: str | None) -> Any:
        """The ``auth`` context a user's access token stands for (anonymous when it is missing, forged or expired). Verified on the deployment."""
        return self._call("POST", self._manage("/runtime/identify"), json={"token": token})

    def runtime_db(self, op: str, **fields: Any) -> Any:
        return self._call("POST", self._manage("/runtime/db"), json={"op": op, **fields})


def _failure(response: httpx.Response) -> PawabaseError:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    return PawabaseError(response.status_code, detail)


class _Base(_Endpoints):
    def __init__(self, url: str, api_key: str, *, environment: str = "development", timeout: float = 30.0, retries: int = 2) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.environment = environment
        self.timeout = timeout
        self.retries = retries
        self._extra: dict[str, str] = {}
        self._borrowed = False

    def __repr__(self) -> str:
        return f"{type(self).__name__}(url={self.url!r}, environment={self.environment!r}, api_key=<hidden>)"

    @property
    def headers(self) -> dict[str, str]:
        # The gateway resolves this key into a signed environment context. The raw key is never forwarded to a function or an internal Pawabase service.
        return {"apikey": self.api_key, "x-environment": self.environment, "user-agent": "pawabase-python/0.2"}

    def as_user(self, token: str | None, **headers: str) -> Any:
        """A view of this client that acts as one signed-in user: the same connection pool, with ``Authorization: Bearer <token>`` (and any extra *headers*) on every request.

        A web server holds one client for the process and takes one of these per request, so users never share a connection's credentials. Closing a view closes nothing.
        """
        import copy

        view = copy.copy(self)
        view._extra = {**({"Authorization": f"Bearer {token}"} if token else {}), **{k.replace("_", "-"): v for k, v in headers.items()}}
        view._borrowed = True
        return view

    @staticmethod
    def _body(response: httpx.Response) -> Any:
        if response.is_error:
            raise _failure(response)
        return response.json() if response.content else None

    def _attempts(self, retry: bool) -> int:
        return 1 + (self.retries if retry else 0)


class Pawabase(_Base):
    """Blocking client.

    Args:
        url: The gateway URL.
        api_key: A publishable or secret key of the environment.
        environment: The environment, such as ``development``.
        timeout: Seconds per request.
        retries: Extra attempts for reads that fail with a connection error or a 502/503/504.
    """

    def __init__(self, url: str, api_key: str, *, environment: str = "development", timeout: float = 30.0, retries: int = 2) -> None:
        super().__init__(url, api_key, environment=environment, timeout=timeout, retries=retries)
        self._client = httpx.Client(base_url=self.url, headers=self.headers, timeout=timeout)

    def close(self) -> None:
        if not self._borrowed:
            self._client.close()

    def __enter__(self) -> Pawabase:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _call(self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None, retry: bool = False) -> Any:
        last: Exception | None = None
        waits = 0
        attempt = 0
        while attempt < self._attempts(retry):
            try:
                response = self._client.request(method, path, json=json, params=params, headers=self._extra or None)
            except httpx.TransportError as error:
                last = PawabaseError(0, f"Could not reach {self.url}: {error}")
            else:
                if response.status_code == 429 and waits < MAX_RATE_WAITS:
                    waits += 1
                    time.sleep(rate_wait(response))
                    continue
                if response.status_code in RETRIED_STATUSES and retry and attempt + 1 < self._attempts(retry):
                    last = _failure(response)
                else:
                    return self._body(response)
            time.sleep(min(0.25 * 2**attempt, 2.0))
            attempt += 1
        assert last is not None
        raise last


class AsyncPawabase(_Base):
    """Async client: the same methods, each returning an awaitable."""

    def __init__(self, url: str, api_key: str, *, environment: str = "development", timeout: float = 30.0, retries: int = 2) -> None:
        super().__init__(url, api_key, environment=environment, timeout=timeout, retries=retries)
        self._client = httpx.AsyncClient(base_url=self.url, headers=self.headers, timeout=timeout)

    async def close(self) -> None:
        if not self._borrowed:
            await self._client.aclose()

    async def download(self, path: str, *, params: Mapping[str, Any] | None = None) -> httpx.Response:
        """``GET`` a path or URL and hand back the raw response (bytes, headers, status), whatever the status: for files, images and signed storage URLs."""
        return await self._client.get(path, params=params, headers=self._extra or None)

    async def __aenter__(self) -> AsyncPawabase:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def _call(self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None, retry: bool = False) -> Any:  # type: ignore[override]
        last: Exception | None = None
        waits = 0
        attempt = 0
        while attempt < self._attempts(retry):
            try:
                response = await self._client.request(method, path, json=json, params=params, headers=self._extra or None)
            except httpx.TransportError as error:
                last = PawabaseError(0, f"Could not reach {self.url}: {error}")
            else:
                if response.status_code == 429 and waits < MAX_RATE_WAITS:
                    waits += 1
                    await asyncio.sleep(rate_wait(response))
                    continue
                if response.status_code in RETRIED_STATUSES and retry and attempt + 1 < self._attempts(retry):
                    last = _failure(response)
                else:
                    return self._body(response)
            await asyncio.sleep(min(0.25 * 2**attempt, 2.0))
            attempt += 1
        assert last is not None
        raise last
