"""Mail: what the environment sent, delivery settings, and templates."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ._base import Definitions, Service


class MailTemplates(Definitions):
    """Templates (subject, HTML, text) for the mail flows, functions and sign-in send."""

    kind = "mail-templates"


class Mail(Service):
    def config(self) -> Any:
        """The delivery settings in effect (the password is never returned) and whether mail is being delivered."""
        return self.get("/mail/config")

    def log(self, *, status: str | None = None, limit: int | None = None) -> Any:
        """Messages sent or suppressed. ``status``: ``sent``, ``failed``, ``suppressed``."""
        return self.get("/mail/log", status=status, limit=limit)

    def send_test(self, to: Sequence[str]) -> Any:
        return self.post("/mail/test", {"to": list(to)})
