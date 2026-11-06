"""Mail: what the environment sent, delivery settings, and templates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ._base import Definitions, Service, seg


class MailTemplates(Definitions):
    """Templates (subject, HTML, text) for the mail flows and sign-in send."""

    kind = "mail-templates"

    def rename(self, name: str, to: str) -> Any:
        """Rename a template, keeping its versions."""
        return self.post(seg(self.kind, name, "rename"), {"to": to})


class Mail(Service):
    def kit(self) -> Any:
        """The brand kit the template editor uses: brand styles, palettes, reusable blocks and folders."""
        return self.get("/mail/kit")

    def save_kit(self, *, brand: Mapping[str, Any] | None = None, palettes: Sequence[Mapping[str, Any]] = (), blocks: Sequence[Mapping[str, Any]] = (), folders: Sequence[str] = ()) -> Any:
        return self.put("/mail/kit", {"brand": dict(brand or {}), "palettes": [dict(p) for p in palettes], "blocks": [dict(b) for b in blocks], "folders": list(folders)})

    def config(self) -> Any:
        """The delivery settings in effect (the password is never returned) and whether mail is being delivered."""
        return self.get("/mail/config")

    def log(self, *, status: str | None = None, limit: int | None = None) -> Any:
        """Messages sent or suppressed. ``status``: ``sent``, ``failed``, ``suppressed``."""
        return self.get("/mail/log", status=status, limit=limit)

    def send_test(self, to: Sequence[str]) -> Any:
        return self.post("/mail/test", {"to": list(to)})
