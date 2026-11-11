"""``pb.captcha``: bot protection for your own forms, and checking a pass on your server.

On a server, with a secret key, check the pass a form sent you::

    result = pb.captcha.verify(form["pawabase-captcha-response"], action="contact", remoteip=request.client.host)
    if not result["success"]:
        raise PermissionError("failed the security check")

In an app or a script with a publishable key, earn a pass the way the widget does: ask, solve, redeem. The built-in check is solved here;
for Turnstile, hCaptcha or reCAPTCHA give ``get_token``, which shows the provider's widget and returns its token::

    granted = pb.captcha.protect("contact")
    send_form(..., captcha=granted["pass"])

A pass also opens a protected auth call: ``pb.auth.sign_in_with_password(..., captcha_token=pb.captcha.protect("signin")["pass"])``.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Callable, Mapping
from typing import Any

from . import _core
from .errors import PawabaseError

PREFIX = "pow1"


def leading_zero_bits(digest: bytes) -> int:
    bits = 0
    for byte in digest:
        if byte == 0:
            bits += 8
            continue
        return bits + (8 - byte.bit_length())
    return bits


def solve(challenge: Mapping[str, Any], *, limit: int = 1 << 28) -> str:
    """Solve the built-in puzzle: the first ``nonce`` whose SHA-256 of ``"<salt>:<nonce>"`` starts with ``bits`` zero bits."""
    if challenge.get("provider") != "pawabase" or not challenge.get("challenge"):
        raise ValueError("this is not a built-in challenge to solve")
    salt, bits = str(challenge["salt"]), int(challenge["bits"])
    for nonce in range(limit):
        if leading_zero_bits(hashlib.sha256(f"{salt}:{nonce}".encode()).digest()) >= bits:
            return f"{PREFIX}.{challenge['challenge']}.{nonce}"
    raise PawabaseError(0, {"error": "captcha_unsolved", "message": "the security check could not be solved"})


def challenge_of(error: Exception) -> dict[str, Any] | None:
    """The check a refused request asks for (``captcha_required`` / ``captcha_failed``), or ``None``."""
    if getattr(error, "code", None) not in ("captcha_required", "captcha_failed"):
        return None
    details = getattr(error, "details", None)
    return details.get("captcha") if isinstance(details, Mapping) else None


class Captcha:
    def __init__(self, client: Any) -> None:
        self._c = client

    def prepare(self, action: str) -> Any:
        """What to solve for an action: ``{"required", "mode", "captcha"}``."""
        return self._c._call("POST", "/auth/v1/captcha/challenge", json={"action": action}, auth=False)

    def redeem(self, action: str, token: str | None) -> Any:
        """Swap what was solved for a pass: ``{"pass", "action", "expires_at"}``."""
        return self._c._call("POST", "/auth/v1/captcha/redeem", json={"action": action, "token": token}, auth=False)

    def protect(self, action: str, *, get_token: Callable[[Mapping[str, Any]], str | None] | None = None) -> Any:
        """Earn a pass for ``action`` (an auth action or your form's name): ask, solve, redeem. A wrong answer is retried once."""

        def answer(challenge: Mapping[str, Any] | None) -> str | None:
            if not challenge or challenge.get("provider") == "none":
                return None
            if challenge.get("provider") == "pawabase":
                return solve(challenge)
            if get_token is None:
                raise PawabaseError(0, {"error": "captcha_widget_needed", "message": f"this environment checks with {challenge.get('provider')}: pass get_token to show its widget"})
            return get_token(challenge)

        def attempt(challenge: Mapping[str, Any] | None, left: int) -> Any:
            def retry(error: PawabaseError) -> Any:
                fresh = challenge_of(error)
                if fresh is None or left == 0:
                    raise error
                return attempt(fresh, left - 1)

            try:
                result = self.redeem(action, answer(challenge))
            except PawabaseError as error:  # the blocking client raises here
                return retry(error)
            if not inspect.isawaitable(result):
                return result

            async def finish() -> Any:
                try:
                    return await result
                except PawabaseError as error:
                    again = retry(error)
                    return await again if inspect.isawaitable(again) else again

            return finish()

        def start(prepared: Mapping[str, Any]) -> Any:
            if prepared.get("unavailable"):
                raise PawabaseError(503, {"error": "captcha_unavailable", "message": "the security check is not available right now; try again in a minute"})
            return attempt(prepared.get("captcha"), 1)

        return _core.chain(self.prepare(action), start)

    def verify(self, token: str, *, action: str | None = None, remoteip: str | None = None) -> Any:
        """Check a pass a form sent you (server only: needs a secret key). Turnstile's shape: ``success``, ``error-codes``, plus
        ``action``, ``hostname``, ``score`` (0 automated … 1 a person) and ``same_address``."""
        return self._c._call(
            "POST", "/auth/v1/captcha/siteverify", json=_core.compact({"response": token, "action": action, "remoteip": remoteip}), auth=False
        )

