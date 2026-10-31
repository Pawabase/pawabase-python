"""Authentication: your users' accounts, sessions, second factors, devices, API keys and organizations.

    pb.auth.sign_up(email="ada@example.com", password="…", name="Ada")
    result = pb.auth.sign_in_with_password(email="ada@example.com", password="…")
    if result.status == "mfa_required":
        result = result.verify("123456")
    pb.auth.get_user()
    pb.auth.sign_out()

A signed-in session is kept in memory (or in a :class:`SessionStore` you pass to the client), its access token goes on every request,
and an expiring or rejected one is renewed with the refresh token. Everything here is the signed-in user's own account; to manage *other*
users with a secret key, use ``pb.admin``.
"""

from __future__ import annotations

import inspect
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import _core
from .storage import _content
from .errors import ConfigError

REFRESH_SKEW = 30  # seconds before expiry at which a token is renewed


@dataclass
class Session:
    """A signed-in session: the tokens, when the access token expires (epoch seconds), and the user."""

    access_token: str
    refresh_token: str
    expires_at: float
    user: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    org: str | None = None
    org_role: str | None = None

    @classmethod
    def of(cls, wire: Mapping[str, Any], fallback: Session | None = None) -> Session:
        user = wire.get("user") or (fallback.user if fallback else {})
        expires_at = wire.get("expires_at") or (time.time() + float(wire.get("expires_in") or 900))
        return cls(
            str(wire["access_token"]), str(wire["refresh_token"]), float(expires_at), dict(user), wire.get("session_id"), wire.get("org"), wire.get("org_role")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token, "refresh_token": self.refresh_token, "expires_at": self.expires_at,
            "user": self.user, "session_id": self.session_id, "org": self.org, "org_role": self.org_role,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Session | None:
        try:
            return cls.of(data)
        except (KeyError, TypeError, ValueError):
            return None


class SessionStore(Protocol):
    """Where a session lives between runs: ``get()`` returns it (or ``None``), ``set(None)`` forgets it."""

    def get(self) -> Session | None: ...

    def set(self, session: Session | None) -> None: ...


class MemorySessionStore:
    def __init__(self) -> None:
        self._session: Session | None = None

    def get(self) -> Session | None:
        return self._session

    def set(self, session: Session | None) -> None:
        self._session = session


class FileSessionStore:
    """A session in a file only its owner can read (a script or a CLI that should stay signed in)."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()

    def get(self) -> Session | None:
        try:
            return Session.from_dict(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            return None

    def set(self, session: Session | None) -> None:
        if session is None:
            self.path.unlink(missing_ok=True)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.write_text(json.dumps(session.to_dict()))


@dataclass
class SignInResult:
    """``status`` is ``signed_in`` (``session`` is set) or ``mfa_required`` (call ``verify(code)`` with a code from the user's factor)."""

    status: str
    session: Session | None = None
    mfa_token: str | None = None
    factors: list[str] = field(default_factory=list)
    _verify: Callable[[str], Any] | None = field(default=None, repr=False)

    def verify(self, code: str) -> Any:
        """Finish an MFA sign-in with the user's code: the session (an awaitable on the async client)."""
        if self._verify is None:
            raise ConfigError("This sign-in does not need a second factor.")
        return self._verify(code)


@dataclass
class SignUpResult:
    user: dict[str, Any]
    session: Session | None
    verification_required: bool


class _Part:
    def __init__(self, auth: Auth) -> None:
        self._auth = auth
        self._c = auth._c


class Auth:
    def __init__(self, client: Any, store: SessionStore | None = None) -> None:
        self._c = client
        self._store: SessionStore = store or MemorySessionStore()
        self._listeners: list[Callable[[str, Session | None], None]] = []
        self.mfa = Mfa(self)
        self.sessions = Sessions(self)
        self.devices = Devices(self)
        self.api_keys = ApiKeys(self)
        self.machine = Machine(self)
        self.orgs = Orgs(self)
        self.passkeys = Passkeys(self)

    # ── the session ──────────────────────────────────────────────────────

    @property
    def session(self) -> Session | None:
        return self._store.get()

    @property
    def access_token(self) -> str | None:
        session = self.session
        return session.access_token if session else None

    @property
    def user(self) -> dict[str, Any] | None:
        session = self.session
        return session.user if session else None

    def expiring(self) -> bool:
        session = self.session
        return bool(session and self._c.auto_refresh and session.expires_at - time.time() < REFRESH_SKEW and session.refresh_token)

    def can_refresh(self) -> bool:
        session = self.session
        return bool(session and session.refresh_token)

    def on_auth_state_change(self, listener: Callable[[str, Session | None], None]) -> Callable[[], None]:
        """Call ``listener(event, session)`` on ``SIGNED_IN``, ``SIGNED_OUT``, ``TOKEN_REFRESHED`` and ``USER_UPDATED``. Returns an unsubscribe function."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _emit(self, event: str, session: Session | None) -> None:
        for listener in list(self._listeners):
            listener(event, session)

    def _keep(self, session: Session | None, event: str) -> Session | None:
        self._store.set(session)
        self._emit(event, session)
        return session

    def set_session(self, access_token: str, refresh_token: str) -> Any:
        """Adopt tokens you already hold (from a cookie, a redirect, another process): the user is fetched and the session kept."""
        self._store.set(Session(access_token, refresh_token, time.time() + 60, {}))
        return _core.then(self._c._call("GET", "/auth/v1/user", retry=True), lambda user: self._keep(Session(access_token, refresh_token, time.time() + 60, user), "SIGNED_IN"))

    def refresh_session(self, *, org: str | None = None) -> Any:
        """Renew the tokens with the refresh token (``org`` switches the organization the tokens are issued for)."""
        session = self.session
        if session is None or not session.refresh_token:
            raise ConfigError("There is no session to refresh: sign in first.")

        def kept(wire: Any) -> Session:
            fresh = Session.of(wire, session)
            self._keep(fresh, "TOKEN_REFRESHED")
            return fresh

        def failed(error: Exception) -> None:
            # A refresh token that is no longer good ends the session; anything else (the network) leaves it for a retry.
            if getattr(error, "status_code", 0) in (400, 401):
                self._keep(None, "SIGNED_OUT")

        body = {"grant_type": "refresh_token", "refresh_token": session.refresh_token, **({"org": org} if org else {})}
        try:
            return _guard(self._c._call("POST", "/auth/v1/token", json=body, auth=False), kept, failed)
        except Exception as error:
            failed(error)
            raise

    # ── signing up and in ────────────────────────────────────────────────

    def settings(self) -> Any:
        """What this environment offers: sign-up, providers, MFA, password policy."""
        return self._c._call("GET", "/auth/v1/settings", retry=True, auth=False)

    def sign_up(
        self, *, email: str, password: str, username: str | None = None, name: str | None = None, data: Mapping[str, Any] | None = None,
        redirect_to: str | None = None, accepted_terms: bool | None = None,
    ) -> Any:
        """Create an account. A session when the environment signs people in at once, or ``verification_required`` when they must confirm their email first."""
        body = _core.compact(
            {"email": email, "password": password, "username": username, "name": name, "data": dict(data) if data else None,
             "redirect_to": redirect_to, "accepted_terms": accepted_terms}
        )

        def done(wire: Any) -> SignUpResult:
            if wire.get("verification_required") or not wire.get("access_token"):
                return SignUpResult(wire.get("user") or {}, None, True)
            session = Session.of(wire)
            self._keep(session, "SIGNED_IN")
            return SignUpResult(session.user, session, False)

        return _core.then(self._c._call("POST", "/auth/v1/signup", json=body, auth=False), done)

    def sign_in_with_password(self, *, email: str, password: str, org: str | None = None, remember_device: bool = False) -> Any:
        body = _core.compact({"grant_type": "password", "email": email, "password": password, "org": org, "remember_device": remember_device or None})
        return _core.then(self._c._call("POST", "/auth/v1/token", json=body, auth=False), lambda wire: self._signed_in(wire, org))

    def _signed_in(self, wire: Mapping[str, Any], org: str | None = None) -> SignInResult:
        if wire.get("mfa_required"):
            token = str(wire["mfa_token"])
            return SignInResult("mfa_required", None, token, list(wire.get("factors") or []), lambda code: self.verify_mfa(mfa_token=token, code=code, org=org))
        session = Session.of(wire)
        self._keep(session, "SIGNED_IN")
        return SignInResult("signed_in", session)

    def verify_mfa(self, *, mfa_token: str, code: str, org: str | None = None, remember_device: bool = False) -> Any:
        """Finish an MFA sign-in: the session."""
        body = _core.compact({"grant_type": "mfa", "mfa_token": mfa_token, "code": code, "org": org, "remember_device": remember_device or None})

        def done(wire: Any) -> Session:
            session = Session.of(wire)
            self._keep(session, "SIGNED_IN")
            return session

        return _core.then(self._c._call("POST", "/auth/v1/token", json=body, auth=False), done)

    def sign_in_with_magic_link(self, *, email: str, redirect_to: str | None = None, create_user: bool | None = None) -> Any:
        """Email a one-time sign-in link."""
        body = _core.compact({"email": email, "redirect_to": redirect_to, "create_user": create_user})
        return self._c._call("POST", "/auth/v1/magic-link", json=body, auth=False)

    def verify_magic_link(self, token: str) -> Any:
        """Exchange the token from a magic link for a session."""
        return _core.then(self._c._call("POST", "/auth/v1/magic-link/verify", json={"token": token}, auth=False), self._signed_in)

    def oauth_url(self, provider: str, *, redirect_to: str | None = None, extra: Mapping[str, str] | None = None) -> str:
        """Where to send a browser for a social sign-in. Needs a publishable key (a secret key must never appear in a URL)."""
        if _core.is_secret_key(self._c.api_key):
            raise ConfigError("Refusing to put a secret key in a URL: use a publishable key for social sign-in.")
        return self._c.url_for(
            f"/auth/v1/authorize/{_core.path_of(self._c.environment)}/{_core.path_of(provider)}", **{"redirect_to": redirect_to, "apikey": self._c.api_key, **(extra or {})}
        )

    def exchange_code(self, code: str, *, code_verifier: str | None = None) -> Any:
        """Trade the ``code`` a sign-in redirect carried for a session (with the verifier of a PKCE ``code_challenge``)."""
        body = _core.compact({"code": code, "code_verifier": code_verifier})

        def done(wire: Any) -> Session:
            session = Session.of(wire)
            self._keep(session, "SIGNED_IN")
            return session

        return _core.then(self._c._call("POST", "/auth/v1/token/exchange", json=body, auth=False), done)

    def sign_out(self, *, scope: str = "local") -> Any:
        """End the session: ``local`` (this one), ``global`` (all of the user's) or ``others``. The local session is forgotten even if the call fails."""
        had = self.can_refresh() or self.access_token
        if not had:
            return None if not self._c.is_async else _noop()
        def finish(_: Any = None) -> None:
            if scope != "others":
                self._keep(None, "SIGNED_OUT")

        try:
            call = self._c._call("POST", "/auth/v1/logout", json={"scope": scope})
        except Exception:
            finish()
            raise
        return _guard(call, finish, lambda error: finish())

    # ── the signed-in user ───────────────────────────────────────────────

    def get_user(self) -> Any:
        def kept(user: Any) -> Any:
            session = self.session
            if session:
                session.user = user
                self._store.set(session)
            return user

        return _core.then(self._c._call("GET", "/auth/v1/user", retry=True), kept)

    def update_user(
        self, *, name: str | None = None, avatar_url: str | None = None, data: Mapping[str, Any] | None = None, password: str | None = None,
        current_password: str | None = None, email: str | None = None,
    ) -> Any:
        """Change the signed-in user: profile fields, ``data`` (merged into ``user_metadata``), a new password (with ``current_password``), or a new email (confirmed by link)."""
        body = _core.compact({"name": name, "avatar_url": avatar_url, "data": dict(data) if data else None, "password": password, "current_password": current_password, "email": email})

        def after(_: Any) -> Any:
            return _core.then(self.get_user(), lambda user: (self._emit("USER_UPDATED", self.session), user)[1])

        return _core.chain(self._c._call("PATCH", "/auth/v1/user", json=body), after)

    def set_avatar(self, image: Any, *, content_type: str | None = None) -> Any:
        """Set the signed-in user's picture from an image (``bytes``, an open file or a ``Path``): PNG, JPEG, WebP or GIF.

        It is stored in the environment's public avatar bucket and ``avatar_url`` points at it; the previous picture is removed.
        """
        data, guessed = _content(image)
        declared = content_type or guessed or "application/octet-stream"

        def after(_: Any) -> Any:
            return _core.then(self.get_user(), lambda user: (self._emit("USER_UPDATED", self.session), user)[1])

        return _core.chain(self._c._call("PUT", "/auth/v1/user/avatar", content=data, headers={"content-type": declared}), after)

    def remove_avatar(self) -> Any:
        """Remove the signed-in user's picture."""

        def after(_: Any) -> Any:
            return _core.then(self.get_user(), lambda user: (self._emit("USER_UPDATED", self.session), user)[1])

        return _core.chain(self._c._call("DELETE", "/auth/v1/user/avatar"), after)

    def resend_verification(self, *, email: str, redirect_to: str | None = None) -> Any:
        return self._c._call("POST", "/auth/v1/verify/request", json=_core.compact({"email": email, "redirect_to": redirect_to}), auth=False)

    def verify_email(self, token: str) -> Any:
        """Confirm an email address from the link's token: signs the user in."""
        def done(wire: Any) -> Session:
            session = Session.of(wire)
            self._keep(session, "SIGNED_IN")
            return session

        return _core.then(self._c._call("POST", "/auth/v1/verify", json={"token": token}, auth=False), done)

    def confirm_email_change(self, token: str) -> Any:
        return self._c._call("POST", "/auth/v1/email/confirm", json={"token": token}, auth=False)

    def request_password_reset(self, *, email: str, redirect_to: str | None = None) -> Any:
        return self._c._call("POST", "/auth/v1/recover", json=_core.compact({"email": email, "redirect_to": redirect_to}), auth=False)

    def reset_password(self, *, token: str, password: str) -> Any:
        """Set a new password from a reset link's token: signs the user in."""
        def done(wire: Any) -> Session:
            session = Session.of(wire)
            self._keep(session, "SIGNED_IN")
            return session

        return _core.then(self._c._call("POST", "/auth/v1/recover/confirm", json={"token": token, "password": password}, auth=False), done)

    # ── linked sign-in methods and the organization in use ──────────────

    def link_identity_url(self, provider: str, *, redirect_to: str | None = None) -> Any:
        """Where to send the signed-in user to link another provider to their account."""
        return _core.then(
            self._c._call("POST", "/auth/v1/identities/link", params=_core.compact({"provider": provider, "redirect_to": redirect_to})), lambda body: body["url"]
        )

    def unlink_identity(self, identity_id: Any) -> Any:
        return self._c._call("DELETE", f"/auth/v1/identities/{_core.path_of(identity_id)}")

    def identities(self) -> Any:
        return _core.then(self.get_user(), lambda user: user.get("identities", []))

    # ── guests, emailed codes, integration tokens ────────────────────────

    def sign_in_anonymously(self) -> Any:
        """Start a guest session: an account with no email or password, for someone who has not signed up yet. Turn it into a real one with
        :meth:`upgrade_anonymous`, keeping everything the guest created. The environment must allow guest sign-in."""
        def done(wire: Any) -> Session:
            session = Session.of(wire)
            self._keep(session, "SIGNED_IN")
            return session

        return _core.then(self._c._call("POST", "/auth/v1/anonymous", json={}, auth=False), done)

    def upgrade_anonymous(self, *, email: str, password: str | None = None, name: str | None = None, redirect_to: str | None = None) -> Any:
        """Turn the signed-in guest into a full account (a verification email is sent): ``{"user", "verification_sent"}``."""
        body = _core.compact({"email": email, "password": password, "name": name, "redirect_to": redirect_to})

        def done(wire: Any) -> Any:
            session = self.session
            if session and wire.get("user"):
                session.user = wire["user"]
                self._store.set(session)
                self._emit("USER_UPDATED", session)
            return wire

        return _core.then(self._c._call("POST", "/auth/v1/anonymous/upgrade", json=body), done)

    def send_email_code(self, *, email: str, create_user: bool = True) -> Any:
        """Email a short sign-in code (the answer is the same whether or not the address has an account)."""
        return self._c._call("POST", "/auth/v1/email-code", json={"email": email, "create_user": create_user}, auth=False)

    def verify_email_code(self, *, email: str, code: str) -> Any:
        """Sign in with the code from :meth:`send_email_code`: a :class:`SignInResult` (``mfa_required`` when the account has a second factor)."""
        return _core.then(self._c._call("POST", "/auth/v1/email-code/verify", json={"email": email, "code": code}, auth=False), self._signed_in)

    def jwks(self) -> Any:
        """The public keys that verify integration tokens (:meth:`issue_token`): ``{"keys": [...]}``, for a server checking them."""
        return self._c._call("GET", f"/auth/v1/token-keys/{_core.path_of(self._c.environment)}/jwks", retry=True, auth=False)

    def issue_token(self, profile: str) -> Any:
        """Exchange the signed-in session for a short-lived token for another service, shaped by a token profile you defined in Studio:
        ``{"token", "token_type", "expires_in", "expires_at", "audience"}``."""
        return self._c._call("POST", "/auth/v1/token-profiles/issue", json={"profile": profile})

    def switch_org(self, slug: str | None) -> Any:
        """Get tokens for another organization of the user (``None`` for none)."""
        return self.refresh_session(org=slug)


def _guard(value: Any, ok: Callable[[Any], Any], failed: Callable[[Exception], Any]) -> Any:
    """:func:`~pawabase._core.then` with an error handler, for a value or an awaitable."""
    if inspect.isawaitable(value):

        async def run() -> Any:
            try:
                return ok(await value)
            except Exception as error:
                failed(error)
                raise

        return run()
    return ok(value)


async def _noop() -> None:
    return None


class Mfa(_Part):
    """Second factors of the signed-in user: an authenticator app, emailed codes, recovery codes."""

    def status(self) -> Any:
        return self._c._call("GET", "/auth/v1/mfa", retry=True)

    def enroll(self) -> Any:
        """Start enrolling an authenticator app: ``factor_id``, ``secret`` and an ``otpauth_uri`` to show as a QR code. Confirm with :meth:`verify_enrollment`."""
        return self._c._call("POST", "/auth/v1/mfa/totp/enroll")

    def verify_enrollment(self, code: str) -> Any:
        """Confirm the app with a code it shows: turns MFA on and returns one-time ``recovery_codes`` (show them once)."""
        return _core.chain(self._c._call("POST", "/auth/v1/mfa/totp/verify", json={"code": code}), lambda result: _core.then(self._auth.refresh_session(), lambda _: result))

    def enroll_email(self) -> Any:
        """Start using emailed codes as a factor (a code is sent to the account's address)."""
        return self._c._call("POST", "/auth/v1/mfa/email/enroll")

    def challenge(self, mfa_token: str) -> Any:
        """Send the emailed code for a sign-in that is waiting on MFA."""
        return self._c._call("POST", "/auth/v1/mfa/challenge", json={"mfa_token": mfa_token}, auth=False)

    def start_setup(self, *, mfa_token: str, method: str) -> Any:
        """When the environment requires MFA, a sign-in is held until a factor is set up: start it (``method`` is ``totp`` or ``email``)."""
        return self._c._call("POST", "/auth/v1/mfa/setup/start", json={"mfa_token": mfa_token, "method": method}, auth=False)

    def complete_setup(self, *, mfa_token: str, code: str, org: str | None = None) -> Any:
        """Finish the required setup with a first code: the session and the recovery codes."""
        def done(wire: Any) -> Any:
            if wire.get("access_token"):
                self._auth._keep(Session.of(wire), "SIGNED_IN")
            return wire

        return _core.then(self._c._call("POST", "/auth/v1/mfa/setup/complete", json=_core.compact({"mfa_token": mfa_token, "code": code, "org": org}), auth=False), done)

    def disable(self, code: str) -> Any:
        return self._c._call("POST", "/auth/v1/mfa/disable", json={"code": code})

    def regenerate_recovery_codes(self, code: str) -> Any:
        return self._c._call("POST", "/auth/v1/mfa/recovery-codes", json={"code": code})


class Sessions(_Part):
    """The user's signed-in sessions."""

    def list(self) -> Any:
        return _core.then(self._c._call("GET", "/auth/v1/sessions", retry=True), lambda body: body["data"])

    def revoke(self, session_id: str) -> Any:
        return self._c._call("DELETE", f"/auth/v1/sessions/{_core.path_of(session_id)}")

    def history(self) -> Any:
        """Recent sign-ins and failures on the account."""
        return _core.then(self._c._call("GET", "/auth/v1/history", retry=True), lambda body: body["data"])


class Devices(_Part):
    """Browsers and apps the user has signed in from."""

    def list(self) -> Any:
        return _core.then(self._c._call("GET", "/auth/v1/user/devices", retry=True), lambda body: body.get("data", body))

    def forget(self, device_id: str) -> Any:
        return self._c._call("DELETE", f"/auth/v1/user/devices/{_core.path_of(device_id)}")


class Passkeys(_Part):
    """Passkeys (WebAuthn) of the signed-in user, and signing in with one.

    The browser (or a native app) makes the credential: ``options`` goes to ``navigator.credentials.create`` / ``.get``, and what it returns
    goes back with the ``ticket``. This part carries both ends over the wire; the TypeScript client does the browser step too.
    """

    def list(self) -> Any:
        return _core.then(self._c._call("GET", "/auth/v1/passkeys", retry=True), lambda body: body["data"])

    def remove(self, passkey_id: str) -> Any:
        return self._c._call("DELETE", f"/auth/v1/passkeys/{_core.path_of(passkey_id)}")

    def register_options(self) -> Any:
        """Begin adding a passkey: ``{"ticket", "options"}``."""
        return self._c._call("POST", "/auth/v1/passkeys/register/options", json={})

    def register_verify(self, *, ticket: str, credential: Mapping[str, Any], name: str | None = None) -> Any:
        """Finish adding a passkey with the credential the device made: ``{"passkey": ...}``."""
        return self._c._call("POST", "/auth/v1/passkeys/register/verify", json=_core.compact({"ticket": ticket, "credential": dict(credential), "name": name}))

    def authenticate_options(self, *, email: str | None = None) -> Any:
        """Begin signing in with a passkey (with an email, only that account's are offered): ``{"ticket", "options"}``."""
        return self._c._call("POST", "/auth/v1/passkeys/authenticate/options", json=_core.compact({"email": email}), auth=False)

    def authenticate_verify(self, *, ticket: str, credential: Mapping[str, Any]) -> Any:
        """Finish signing in with the credential the device made: the session."""
        def done(wire: Any) -> Session:
            session = Session.of(wire)
            self._auth._keep(session, "SIGNED_IN")
            return session

        return _core.then(self._c._call("POST", "/auth/v1/passkeys/authenticate/verify", json={"ticket": ticket, "credential": dict(credential)}, auth=False), done)


class Machine(_Part):
    """Machines (services and AI agents) signing in with a client id and a key (``pbm_…``)."""

    def token(self, *, client_id: str, client_secret: str, scope: str | None = None) -> Any:
        """The OAuth ``client_credentials`` grant: an access token for the machine, keeping no session.

        The answer has ``access_token``, ``token_type``, ``expires_in`` (seconds), ``scope`` (the permissions it carries) and ``machine``.
        ``scope`` (space-separated permissions) asks for fewer permissions than the key allows; it never grants more.
        """
        body = _core.compact({"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret, "scope": scope})
        return self._c._call("POST", "/auth/v1/token", json=body, auth=False)


class ApiKeys(_Part):
    """Keys a user makes for themselves to call the API as themselves (``pbu_…``): scripts, integrations."""

    def list(self) -> Any:
        return _core.then(self._c._call("GET", "/auth/v1/user/api-keys", retry=True), lambda body: body.get("data", body))

    def create(self, *, name: str, scopes: list[str] | None = None, expires_in_days: int | None = None, org: str | None = None) -> Any:
        """Make a key. The secret is in the answer once and never again."""
        return self._c._call("POST", "/auth/v1/user/api-keys", json=_core.compact({"name": name, "scopes": scopes or [], "expires_in_days": expires_in_days, "org": org}))

    def revoke(self, key_id: str) -> Any:
        return self._c._call("DELETE", f"/auth/v1/user/api-keys/{_core.path_of(key_id)}")


class Orgs(_Part):
    """Organizations, their members, invitations and teams."""

    @staticmethod
    def _base(slug: str) -> str:
        return f"/auth/v1/orgs/{_core.path_of(slug)}"

    def list(self) -> Any:
        return _core.then(self._c._call("GET", "/auth/v1/orgs", retry=True), lambda body: body["data"])

    def create(self, *, slug: str, name: str, metadata: Mapping[str, Any] | None = None) -> Any:
        return self._c._call("POST", "/auth/v1/orgs", json=_core.compact({"slug": slug, "name": name, "metadata": dict(metadata) if metadata else None}))

    def get(self, slug: str) -> Any:
        return self._c._call("GET", self._base(slug), retry=True)

    def update(self, slug: str, *, name: str | None = None, metadata: Mapping[str, Any] | None = None) -> Any:
        return self._c._call("PATCH", self._base(slug), json=_core.compact({"name": name, "metadata": dict(metadata) if metadata else None}))

    def delete(self, slug: str) -> Any:
        return self._c._call("DELETE", self._base(slug))

    def members(self, slug: str) -> Any:
        return _core.then(self._c._call("GET", f"{self._base(slug)}/members", retry=True), lambda body: body["data"])

    def set_member_role(self, slug: str, user_id: str, role: str) -> Any:
        """``role`` is ``viewer``, ``member``, ``admin`` or ``owner``."""
        return self._c._call("PUT", f"{self._base(slug)}/members/{_core.path_of(user_id)}", json={"role": role})

    def remove_member(self, slug: str, user_id: str) -> Any:
        return self._c._call("DELETE", f"{self._base(slug)}/members/{_core.path_of(user_id)}")

    def invite(self, slug: str, *, email: str, role: str = "member", redirect_to: str | None = None) -> Any:
        return self._c._call("POST", f"{self._base(slug)}/invitations", json=_core.compact({"email": email, "role": role, "redirect_to": redirect_to}))

    def invitations(self, slug: str) -> Any:
        return _core.then(self._c._call("GET", f"{self._base(slug)}/invitations", retry=True), lambda body: body["data"])

    def revoke_invitation(self, slug: str, invitation_id: Any) -> Any:
        return self._c._call("DELETE", f"{self._base(slug)}/invitations/{_core.path_of(invitation_id)}")

    def accept_invitation(self, token: str) -> Any:
        return self._c._call("POST", "/auth/v1/invitations/accept", json={"token": token})

    def teams(self, slug: str) -> Any:
        return _core.then(self._c._call("GET", f"{self._base(slug)}/teams", retry=True), lambda body: body["data"])

    def create_team(self, slug: str, *, team_slug: str, name: str) -> Any:
        return self._c._call("POST", f"{self._base(slug)}/teams", json={"slug": team_slug, "name": name})

    def add_team_member(self, slug: str, team: str, user_id: str) -> Any:
        return self._c._call("PUT", f"{self._base(slug)}/teams/{_core.path_of(team)}/members/{_core.path_of(user_id)}")

    def remove_team_member(self, slug: str, team: str, user_id: str) -> Any:
        return self._c._call("DELETE", f"{self._base(slug)}/teams/{_core.path_of(team)}/members/{_core.path_of(user_id)}")
