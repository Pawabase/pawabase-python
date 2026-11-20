"""Connect your product to Pawabase on a person's behalf, and manage their organization's projects.

Two classes, which map onto the two halves of an integration:

* :class:`OAuth` is **your registered app** (its client id and secret). It sends people to Pawabase to approve a connection, trades the code
  for tokens, keeps them fresh and ends the connection.
* :class:`Cloud` is **one connection**: a token for one organization. It calls the Management API: projects, marketplace apps, API keys,
  and everything Studio can do inside a project.

::

    from pawabase.cloud import OAuth, Cloud

    oauth = OAuth(client_id="pbc_…", client_secret="pbs_…", redirect_uri="https://app.example.com/oauth/pawabase/callback")

    # 1. Send the person to Pawabase (keep state and code_verifier in their session)
    pending = oauth.authorize(scopes=["projects:read", "projects:write"])
    redirect(pending.url)

    # 2. They come back to your callback: check the state, trade the code
    tokens = oauth.callback(request.url, state=pending.state, code_verifier=pending.code_verifier)

    # 3. Use the connection. Tokens are refreshed for you, and each refresh is handed to you to save.
    with Cloud(token=oauth.token_source(tokens, on_refresh=save)) as pawabase:
        projects = pawabase.projects.list()

:class:`AsyncOAuth` and :class:`AsyncCloud` are the same with every method awaited.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import secrets
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx

from . import _core
from .errors import PawabaseError

PAWABASE_CLOUD_URL = "https://dashboard.pawabase.com"
SCOPES = ("organizations:read", "projects:read", "projects:write", "project:read", "project:write")


class OAuthError(RuntimeError):
    """An answer from ``/oauth/*`` that says no. ``code`` is the standard OAuth code: ``invalid_grant``, ``access_denied``, ``slow_down``…

    ``needs_reconnect`` is true for ``invalid_grant``: the connection is over (disconnected, expired, or a copy was used), so ask the person to
    connect again. ``status`` is ``0`` when the person was sent back with an error and no request was made.
    """

    def __init__(self, code: str, description: str, status: int = 0, retry_after: float | None = None) -> None:
        super().__init__(description)
        self.code, self.description, self.status, self.retry_after = code, description, status, retry_after

    @property
    def needs_reconnect(self) -> bool:
        return self.code == "invalid_grant"


class CloudError(PawabaseError):
    """An answer from ``/v1/*`` that says no. ``code`` is the API's code (``insufficient_scope``, ``plan_limit``, ``not_found``…) and
    ``details`` what it added: the scope that was needed, the plan limit, the fields that were wrong."""

    def __init__(self, status: int, body: Any) -> None:
        super().__init__(status, body)


@dataclass
class OAuthTokens:
    """What a connection holds. Keep it on your server with your own user record, and treat it like a password."""

    access_token: str
    refresh_token: str | None
    #: When the access token stops working, in seconds since the epoch (already 30 seconds early).
    expires_at: float
    scope: list[str] = field(default_factory=list)
    #: The slug of the organization the person picked (the app's own, for client credentials).
    organization: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OAuthTokens:
        return cls(**{k: data.get(k) for k in ("access_token", "refresh_token", "expires_at", "scope", "organization")})


@dataclass
class PendingAuthorization:
    """What to keep in the person's session between sending them to Pawabase and their coming back."""

    #: Send the browser here (a 302).
    url: str
    state: str
    code_verifier: str


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def pkce_challenge(verifier: str) -> str:
    """The PKCE challenge for a verifier: its SHA-256, base64url without padding."""
    return _b64url(hashlib.sha256(verifier.encode()).digest())


# ── OAuth ──────────────────────────────────────────────────────────────────


class _OAuthBase:
    _is_async = False

    def __init__(self, *, client_id: str, client_secret: str | None = None, redirect_uri: str | None = None, base_url: str = PAWABASE_CLOUD_URL, timeout: float = 30.0, transport: Any = None) -> None:
        if not client_id:
            raise ValueError("client_id is required (the pbc_… id of your app)")
        self.client_id, self._secret, self.redirect_uri = client_id, client_secret, redirect_uri
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self._http: Any = self._make(timeout)

    # -- steps that need no network --

    def authorize(self, *, scopes: Sequence[str] | None = None, state: str | None = None, redirect_uri: str | None = None) -> PendingAuthorization:
        """Step 1: the address to send the person to. It makes the ``state`` (against forged returns) and the PKCE verifier (so a stolen
        code is useless); keep both in the person's session and give them back to :meth:`callback`."""
        target = redirect_uri or self.redirect_uri
        if not target:
            raise ValueError("redirect_uri is required: pass it to the constructor or to authorize()")
        state = state or secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(48)  # 64 characters
        query = {
            "client_id": self.client_id,
            "redirect_uri": target,
            "response_type": "code",
            "state": state,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
        if scopes:
            query["scope"] = " ".join(scopes)
        return PendingAuthorization(f"{self.base_url}/oauth/authorize?{urlencode(query)}", state, verifier)

    def _check_return(self, returned: str | dict[str, Any], state: str) -> str:
        query = returned if isinstance(returned, dict) else {k: v[0] for k, v in parse_qs(urlparse(returned).query).items()}
        if query.get("error"):
            raise OAuthError(query["error"], query.get("error_description") or query["error"])
        if not state or query.get("state") != state:
            raise OAuthError("invalid_state", "This sign-in was not started here.")
        if not query.get("code"):
            raise OAuthError("invalid_request", "Pawabase did not send a code.")
        return query["code"]

    def _tokens(self, response: httpx.Response) -> OAuthTokens:
        if response.status_code >= 400:
            raise self._failure(response)
        reply = response.json()
        return OAuthTokens(
            access_token=reply["access_token"],
            refresh_token=reply.get("refresh_token"),
            expires_at=time.time() + reply["expires_in"] - 30,
            scope=(reply.get("scope") or "").split(),
            organization=reply.get("organization"),
        )

    @staticmethod
    def _failure(response: httpx.Response) -> OAuthError:
        try:
            body = response.json()
        except ValueError:
            body = {}
        wait = response.headers.get("retry-after")
        return OAuthError(
            body.get("error", "server_error"),
            body.get("error_description") or f"Pawabase answered {response.status_code}",
            response.status_code,
            float(wait) if wait and wait.replace(".", "", 1).isdigit() else None,
        )

    def _form(self, path: str, form: dict[str, str]) -> Any:
        data, auth = dict(form), None
        if self._secret:
            auth = httpx.BasicAuth(self.client_id, self._secret)
        else:
            data["client_id"] = self.client_id
        return self._http.post(f"{self.base_url}{path}", data=data, auth=auth, headers={"accept": "application/json"})

    def _token(self, form: dict[str, str]) -> Any:
        return _core.then(self._form("/oauth/token", form), self._tokens)

    # -- steps that call Pawabase (a value, or an awaitable on the async class) --

    def metadata(self) -> Any:
        """The server's own description of itself (RFC 8414): its endpoints, grants and scopes."""
        return _core.then(self._http.get(f"{self.base_url}/.well-known/oauth-authorization-server"), lambda r: r.json())

    def callback(self, returned: str | dict[str, Any], *, state: str, code_verifier: str, redirect_uri: str | None = None) -> Any:
        """Step 2, for your callback route: read what Pawabase sent back (the full address, or its query as a dict), check it is the sign-in
        you started, and trade the code for tokens. Raises :class:`OAuthError` if the person declined, the state is wrong, or the code is refused.

        Never retry this on a network error: a code works once, and a second use ends the connection. Start the sign-in again instead.
        """
        code = self._check_return(returned, state)
        return self.exchange_code(code, code_verifier=code_verifier, redirect_uri=redirect_uri)

    def exchange_code(self, code: str, *, code_verifier: str | None = None, redirect_uri: str | None = None) -> Any:
        """Trade a code for tokens. Prefer :meth:`callback`, which checks the state first."""
        target = redirect_uri or self.redirect_uri
        form = {"grant_type": "authorization_code", "code": code}
        if target:
            form["redirect_uri"] = target
        if code_verifier:
            form["code_verifier"] = code_verifier
        return self._token(form)

    def refresh(self, refresh_token: str) -> Any:
        """Trade a refresh token for a new pair. **The old refresh token stops working**: save the new one before using the new access
        token. Two refreshes of one connection at once end it: refresh through :meth:`token_source`, which keeps them to one at a time."""
        return self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def client_credentials(self, scopes: Sequence[str] | None = None) -> Any:
        """A platform app acting on its own organization, with no person involved. Needs the client secret. The token has no refresh
        token: ask again when it expires."""
        return self._token({"grant_type": "client_credentials", **({"scope": " ".join(scopes)} if scopes else {})})

    def revoke(self, token: str) -> Any:
        """End the connection, so it disappears from the organization's Connected apps. Send the refresh token to end the whole connection;
        an access token ends only that token. Always succeeds for a known client, even for a token Pawabase doesn't recognise."""

        def done(response: httpx.Response) -> None:
            if response.status_code >= 400:
                raise self._failure(response)

        return _core.then(self._form("/oauth/revoke", {"token": token}), done)


class OAuth(_OAuthBase):
    """Your registered app, talking to Pawabase's OAuth server. Every method blocks."""

    def _make(self, timeout: float) -> httpx.Client:
        return httpx.Client(timeout=timeout, transport=self._transport)

    def __enter__(self) -> OAuth:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def token_source(self, tokens: OAuthTokens, *, on_refresh: Callable[[OAuthTokens], Any] | None = None) -> Callable[[], str]:
        """A function that always returns a working access token, for :class:`Cloud`. It refreshes a minute before expiry, one refresh at a
        time per connection in this process (across processes, hold a lock of your own around the storage ``on_refresh`` writes to), and
        calls ``on_refresh`` with each new pair so you can save it before it is used."""
        lock, box = threading.Lock(), {"tokens": tokens}

        def get() -> str:
            with lock:
                current = box["tokens"]
                if time.time() < current.expires_at - 30:
                    return current.access_token
                if not current.refresh_token:
                    raise OAuthError("invalid_grant", "The access token has expired and there is no refresh token.", 400)
                box["tokens"] = self.refresh(current.refresh_token)
                if on_refresh:
                    on_refresh(box["tokens"])
                return box["tokens"].access_token

        return get


class AsyncOAuth(_OAuthBase):
    """The same, with every method awaited."""

    _is_async = True

    def _make(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._transport)

    async def __aenter__(self) -> AsyncOAuth:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def token_source(self, tokens: OAuthTokens, *, on_refresh: Callable[[OAuthTokens], Any] | None = None) -> Callable[[], Awaitable[str]]:
        """Like :meth:`OAuth.token_source`: an async function, with refreshes one at a time. ``on_refresh`` may be async."""
        lock, box = asyncio.Lock(), {"tokens": tokens}

        async def get() -> str:
            async with lock:
                current = box["tokens"]
                if time.time() < current.expires_at - 30:
                    return current.access_token
                if not current.refresh_token:
                    raise OAuthError("invalid_grant", "The access token has expired and there is no refresh token.", 400)
                box["tokens"] = await self.refresh(current.refresh_token)
                if on_refresh:
                    out = on_refresh(box["tokens"])
                    if inspect.isawaitable(out):
                        await out
                return box["tokens"].access_token

        return get


# ── the Management API ─────────────────────────────────────────────────────


def _seg(value: str) -> str:
    return quote(str(value), safe="")


class _Resource:
    def __init__(self, cloud: _CloudBase) -> None:
        self._c = cloud


class Organizations(_Resource):
    def list(self) -> Any:
        """The token's organization, in a list. Scope ``organizations:read``."""
        return self._c.request("GET", "/organizations")

    def get(self, slug: str) -> Any:
        """The organization with its plan and usage. ``slug`` must be the token's own. Scope ``organizations:read``."""
        return self._c.request("GET", f"/organizations/{_seg(slug)}")


class Marketplace(_Resource):
    def list(self) -> Any:
        """Every ready-made app. Scope ``projects:read``."""
        return self._c.request("GET", "/marketplace")

    def get(self, slug: str) -> Any:
        """One app in full, including the variables it asks for."""
        return self._c.request("GET", f"/marketplace/{_seg(slug)}")


class Projects(_Resource):
    def list(self) -> Any:
        """Scope ``projects:read``."""
        return self._c.request("GET", "/projects")

    def get(self, ref: str) -> Any:
        """One project, with its environments. Scope ``projects:read``."""
        return self._c.request("GET", f"/projects/{_seg(ref)}")

    def create(self, name: str, *, description: str | None = None, region: str | None = None, app: str | None = None, values: dict[str, str] | None = None) -> Any:
        """Scope ``projects:write``, role member. Answers at once with the project in state ``creating``: use :meth:`wait_until_running`.

        ``app`` is a marketplace app's slug, installed as soon as the project is running; ``values`` are the app's variables.
        """
        return self._c.request("POST", "/projects", body=_core.compact({"name": name, "description": description, "region": region, "app": app, "values": values}))

    def update(self, ref: str, *, name: str | None = None, description: str | None = None) -> Any:
        """Rename, or change the description (an empty one clears it). Scope ``projects:write``, role admin."""
        return self._c.request("PATCH", f"/projects/{_seg(ref)}", body=_core.compact({"name": name, "description": description}))

    def delete(self, ref: str) -> Any:
        """Delete the project **and all its data**. It cannot be undone, and a protected project is refused (``protected``). Role admin."""
        return self._c.request("DELETE", f"/projects/{_seg(ref)}")

    def pause(self, ref: str) -> Any:
        return self._c.request("POST", f"/projects/{_seg(ref)}/pause")

    def restore(self, ref: str) -> Any:
        return self._c.request("POST", f"/projects/{_seg(ref)}/restore")

    def claim_link(self, ref: str) -> Any:
        """A one-time link that hands a project to a person's own organization: ``{"claim_url", "expires_at"}``. Only a platform app's own
        token may call it."""
        return self._c.request("POST", f"/projects/{_seg(ref)}/claim")

    def install_app(self, ref: str, app: str, values: dict[str, str] | None = None) -> Any:
        """Install a marketplace app into an existing, empty project. Poll :meth:`get` and read ``app.state``, or use :meth:`wait_for_app`."""
        return self._c.request("POST", f"/projects/{_seg(ref)}/apps", body=_core.compact({"app": app, "values": values}))

    def list_keys(self, ref: str) -> Any:
        """The project's API keys (never their secret values). Scope ``project:read``, role member."""
        return _core.then(self._c.request("GET", f"/projects/{_seg(ref)}/api-keys"), lambda body: body["data"])

    def create_key(
        self,
        ref: str,
        name: str,
        *,
        role: str | None = None,
        environments: str | Sequence[str] | None = None,
        scopes: Sequence[str] | None = None,
        expires_at: str | None = None,
        allowed_ips: Sequence[str] | None = None,
        allowed_routes: Sequence[str] | None = None,
        environment: str | None = None,
    ) -> Any:
        """Make an API key for the project's own API. **The key is in the answer once**, as ``key``: store it now. Scope ``project:write``, role admin."""
        body = _core.compact(
            {"name": name, "role": role, "environments": environments if isinstance(environments, str) else (list(environments) if environments else None),
             "scopes": list(scopes) if scopes else None, "expires_at": expires_at, "allowed_ips": list(allowed_ips) if allowed_ips else None,
             "allowed_routes": list(allowed_routes) if allowed_routes else None}
        )
        return self._c.request("POST", f"/projects/{_seg(ref)}/api-keys", body=body, query={"environment": environment})

    def wait_until_running(self, ref: str, *, interval: float = 2.0, timeout: float = 120.0) -> Any:
        """Wait until a new project is ``running``. Raises if it ends up ``failed`` or ``paused``, or if the wait runs out."""

        def check(project: dict[str, Any]) -> None:
            if project["state"] != "running":
                raise RuntimeError(f"Project {ref} ended up {project['state']}")

        return self._until(ref, lambda p: p["state"] != "creating", check, interval, timeout)

    def wait_for_app(self, ref: str, *, interval: float = 2.0, timeout: float = 120.0) -> Any:
        """Wait until the project's marketplace app is ``installed``. Raises with the reason if it ``failed``."""

        def check(project: dict[str, Any]) -> None:
            if (project.get("app") or {}).get("state") == "failed":
                raise RuntimeError(f"The app failed to install: {project['app'].get('error') or 'no reason given'}")

        return self._until(ref, lambda p: (p.get("app") or {}).get("state") in ("installed", "failed"), check, interval, timeout)

    def _until(self, ref: str, done: Callable[[dict[str, Any]], bool], check: Callable[[dict[str, Any]], None], interval: float, timeout: float) -> Any:
        deadline = time.monotonic() + timeout

        def late() -> RuntimeError:
            return RuntimeError(f"Project {ref} was not ready after {timeout:g} seconds")

        if self._c._is_async:

            async def poll() -> dict[str, Any]:
                while True:
                    project = await self.get(ref)
                    if done(project):
                        check(project)
                        return project
                    if time.monotonic() + interval > deadline:
                        raise late()
                    await asyncio.sleep(interval)

            return poll()
        while True:
            project = self.get(ref)
            if done(project):
                check(project)
                return project
            if time.monotonic() + interval > deadline:
                raise late()
            time.sleep(interval)


class ProjectStudio(_Resource):
    """Everything Studio can do inside one project, as plain calls, through ``/v1/projects/{ref}/studio/…``. The answer is the project
    server's own, unchanged; an error keeps its status and raises :class:`CloudError` with the project's own body in ``details``.

    ``target`` is one of ``platform``, ``auth``, ``realtime``, ``telemetry``, ``gateway``, ``object``, ``avatar``, ``document``. Reads need
    ``project:read``, writes ``project:write``::

        env = project["default_environment"]
        studio = pawabase.studio(ref)
        studio.post("platform", f"envs/{env}/resources", {"name": "orders", "fields": [{"name": "total", "type": "number"}]})
    """

    def __init__(self, cloud: _CloudBase, ref: str) -> None:
        super().__init__(cloud)
        self.ref = ref

    def request(self, method: str, target: str, path: str, *, body: Any = None, query: dict[str, Any] | None = None) -> Any:
        return self._c.request(method, f"/projects/{_seg(self.ref)}/studio/{target}/{path.lstrip('/')}", body=body, query=query)

    def get(self, target: str, path: str, query: dict[str, Any] | None = None) -> Any:
        return self.request("GET", target, path, query=query)

    def post(self, target: str, path: str, body: Any = None) -> Any:
        return self.request("POST", target, path, body={} if body is None else body)

    def put(self, target: str, path: str, body: Any = None) -> Any:
        return self.request("PUT", target, path, body=body)

    def patch(self, target: str, path: str, body: Any = None) -> Any:
        return self.request("PATCH", target, path, body=body)

    def delete(self, target: str, path: str) -> Any:
        return self.request("DELETE", target, path)


class _CloudBase:
    _is_async = False

    def __init__(self, *, token: str | Callable[[], Any], base_url: str = PAWABASE_CLOUD_URL, timeout: float = 130.0, transport: Any = None) -> None:
        if not token:
            raise ValueError("token is required")
        self._token = token if callable(token) else (lambda: token)
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self._http: Any = self._make(timeout)
        self.organizations = Organizations(self)
        self.projects = Projects(self)
        self.marketplace = Marketplace(self)

    def studio(self, ref: str) -> ProjectStudio:
        """Everything Studio can do inside one project. See :class:`ProjectStudio`."""
        return ProjectStudio(self, ref)

    def whoami(self) -> Any:
        """What this token is: its organization, role, scopes, and who or what it acts for. Needs no scope. Call it right after connecting."""
        return self.request("GET", "/token")

    @staticmethod
    def _answer(response: httpx.Response) -> Any:
        try:
            data = response.json() if response.content else None
        except ValueError:
            data = response.text
        if response.status_code >= 400:
            raise CloudError(response.status_code, data if isinstance(data, dict) else {"error": "error", "message": response.reason_phrase})
        return data

    def _send(self, method: str, path: str, token: str, body: Any, query: dict[str, Any] | None) -> Any:
        headers = {"authorization": f"Bearer {token}", "accept": "application/json"}
        return self._http.request(method, f"{self.base_url}/v1{path}", headers=headers, json=body, params=_core.compact(query or {}) or None)

    def request(self, method: str, path: str, *, body: Any = None, query: dict[str, Any] | None = None) -> Any:
        """One call to ``/v1``. The answer is the parsed JSON (or ``None`` for an empty one); an error answer raises :class:`CloudError`.
        Use it for an endpoint this class has no method for yet."""
        raise NotImplementedError


class Cloud(_CloudBase):
    """One connection to Pawabase Cloud: the Management API for one organization. Every method blocks.

    ``token`` is a token (an OAuth access token, a personal ``pbp_…`` token, or a platform app's token), or a function returning one:
    see :meth:`OAuth.token_source`.
    """

    def _make(self, timeout: float) -> httpx.Client:
        return httpx.Client(timeout=timeout, transport=self._transport)

    def request(self, method: str, path: str, *, body: Any = None, query: dict[str, Any] | None = None) -> Any:
        return self._answer(self._send(method, path, self._token(), body, query))

    def __enter__(self) -> Cloud:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()


class AsyncCloud(_CloudBase):
    """The same, with every method awaited. ``token`` may be an async function."""

    _is_async = True

    def _make(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._transport)

    async def request(self, method: str, path: str, *, body: Any = None, query: dict[str, Any] | None = None) -> Any:
        token = self._token()
        if inspect.isawaitable(token):
            token = await token
        return self._answer(await self._send(method, path, token, body, query))

    async def __aenter__(self) -> AsyncCloud:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()
