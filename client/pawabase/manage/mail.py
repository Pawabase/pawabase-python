"""Mail: what the environment sent, delivery settings, and templates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
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

    def log(self, *, status: str | None = None, template: str | None = None, q: str | None = None, limit: int | None = None, offset: int | None = None) -> Any:
        """Messages sent, scheduled or suppressed, newest first. ``status``: ``sent``, ``failed``, ``suppressed``, ``scheduled``, ``cancelled``;
        ``template`` limits to one template; ``q`` searches subjects and recipients."""
        return self.get("/mail/log", status=status, template=template, q=q, limit=limit, offset=offset)

    def message(self, message_id: Any) -> Any:
        """One message with everything it carried."""
        return self.get(seg("mail", "log", str(message_id)))

    def retry(self, message_id: Any) -> Any:
        """Send a message again, as a new message."""
        return self.post(seg("mail", "log", str(message_id), "retry"))

    def cancel(self, message_id: Any) -> Any:
        """Cancel a scheduled message before it goes out."""
        return self.post(seg("mail", "log", str(message_id), "cancel"))

    def stats(self, *, days: int | None = None) -> Any:
        """Totals by status, a daily series and recent errors over ``days`` (1 to 365, default 30)."""
        return self.get("/mail/stats", days=days)

    def template_usage(self) -> Any:
        """How often each template was sent, and which flows send it."""
        return self.get("/mail/templates/usage")

    def send(
        self, *, to: Sequence[str], cc: Sequence[str] = (), bcc: Sequence[str] = (), template: str | None = None, data: Mapping[str, Any] | None = None,
        subject: str | None = None, html: str | None = None, text: str | None = None, from_email: str | None = None, reply_to: str | None = None,
        attachments: Sequence[Mapping[str, Any]] = (), scheduled_at: datetime | str | None = None,
    ) -> Any:
        """Send a message: a published ``template`` with ``data``, or ``subject`` with ``html`` / ``text``. ``attachments`` are
        ``{filename, content_type, content}`` with ``content`` in base64 (10 MB in all). ``scheduled_at`` sends later."""
        body: dict[str, Any] = {"to": list(to), "cc": list(cc), "bcc": list(bcc), "data": dict(data or {}), "attachments": [dict(a) for a in attachments]}
        for key, value in (("template", template), ("subject", subject), ("html", html), ("text", text), ("from_email", from_email), ("reply_to", reply_to)):
            if value is not None:
                body[key] = value
        if scheduled_at is not None:
            body["scheduled_at"] = scheduled_at.isoformat() if isinstance(scheduled_at, datetime) else scheduled_at
        return self.post("/mail/send", body)

    def send_test(self, to: Sequence[str]) -> Any:
        return self.post("/mail/test", {"to": list(to)})
