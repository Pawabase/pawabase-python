"""Files: buckets of objects, under each bucket's policies.

    avatars = pb.storage.from_("avatars")
    avatars.upload("ada.png", open("ada.png", "rb"), content_type="image/png")
    data = avatars.download("ada.png")
    avatars.list(prefix="2026/")
    url = avatars.create_signed_url("ada.png", expires_in=600)["url"]

A bucket marked public serves its files without a key at ``public_url``. Resize an image by adding ``width``, ``height``, ``fit``, ``format``
and ``quality`` to its URL (see :meth:`Bucket.transform_url`).
"""

from __future__ import annotations

import json
import mimetypes
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, BinaryIO

import httpx

from . import _core
from .errors import ConfigError

Body = bytes | bytearray | memoryview | str | BinaryIO | Path


def _content(body: Body) -> tuple[Any, str | None]:
    """The bytes to send and a content type guessed from them."""
    if isinstance(body, str):
        return body.encode(), "text/plain; charset=utf-8"
    if isinstance(body, Path):
        return body.read_bytes(), mimetypes.guess_type(body.name)[0]
    if hasattr(body, "read"):
        return body.read(), None
    return bytes(body), None


class Storage:
    """Buckets, and a handle on each."""

    def __init__(self, client: Any) -> None:
        self._c = client

    def list_buckets(self) -> Any:
        return _core.then(self._c._call("GET", "/storage/v1/buckets", retry=True), lambda body: body["data"])

    def from_(self, bucket: str) -> Bucket:
        return Bucket(self._c, bucket)

    bucket = from_


class Bucket:
    def __init__(self, client: Any, name: str) -> None:
        self._c = client
        self.name = name

    def _object(self, key: str) -> str:
        return f"/storage/v1/object/{_core.path_of(self.name)}/{_core.key_path(key)}"

    # ── objects ──────────────────────────────────────────────────────────

    def upload(self, key: str, body: Body, *, content_type: str | None = None) -> Any:
        """Store *body* at *key* (replacing what is there, which is safe to repeat). Returns ``{bucket, key, size, content_type}``."""
        data, guessed = _content(body)
        declared = content_type or guessed or mimetypes.guess_type(key)[0] or "application/octet-stream"
        return self._c._call("PUT", self._object(key), content=data, headers={"content-type": declared}, retry=True)

    def download(self, key: str) -> Any:
        """The object's bytes."""
        return _core.then(self._c._call("GET", self._object(key), raw=True, headers={"accept": "*/*"}, retry=True), lambda response: response.content)

    def download_text(self, key: str, encoding: str = "utf-8") -> Any:
        return _core.then(self.download(key), lambda data: data.decode(encoding))

    def download_json(self, key: str) -> Any:
        return _core.then(self.download(key), lambda data: json.loads(data))

    def download_response(self, key: str) -> Any:
        """The raw ``httpx.Response`` (headers, status, content), for ``Content-Type``, ``ETag`` or ranges."""
        return self._c._call("GET", self._object(key), raw=True, headers={"accept": "*/*"}, retry=True)

    def remove(self, key: str) -> Any:
        return self._c._call("DELETE", self._object(key))

    def list(self, *, prefix: str | None = None, cursor: str | None = None, limit: int | None = None) -> Any:
        """One page of objects: ``{files, prefixes, cursor}`` (``cursor`` is empty on the last page)."""
        return self._c._call("GET", f"/storage/v1/list/{_core.path_of(self.name)}", params=_core.compact({"prefix": prefix, "cursor": cursor, "limit": limit}), retry=True)

    def list_all(self, *, prefix: str | None = None, page_size: int | None = None) -> Any:
        """Every object under *prefix*, page after page (an iterator; an async iterator on the async client)."""
        if self._c.is_async:
            return self._list_all_async(prefix, page_size)
        return self._list_all(prefix, page_size)

    def _list_all(self, prefix: str | None, page_size: int | None) -> Iterator[dict[str, Any]]:
        cursor = ""
        while True:
            page = self.list(prefix=prefix, cursor=cursor or None, limit=page_size)
            yield from page["files"]
            cursor = page.get("cursor") or ""
            if not cursor:
                return

    async def _list_all_async(self, prefix: str | None, page_size: int | None) -> Any:
        cursor = ""
        while True:
            page = await self.list(prefix=prefix, cursor=cursor or None, limit=page_size)
            for entry in page["files"]:
                yield entry
            cursor = page.get("cursor") or ""
            if not cursor:
                return

    # ── URLs ─────────────────────────────────────────────────────────────

    def public_url(self, key: str) -> str:
        """A URL for an object of a *public* bucket, needing no key. (A private bucket answers 404: use :meth:`create_signed_url`.)"""
        return self._c.url_for(f"/storage/v1/files/{_core.path_of(self._c.environment)}/{_core.path_of(self.name)}/{_core.key_path(key)}")

    def transform_url(self, key: str, *, width: int | None = None, height: int | None = None, fit: str | None = None, format: str | None = None, quality: int | None = None) -> str:
        """The public URL of a resized or re-encoded image (``fit``: ``cover``, ``contain``, ``fill``, ``inside``, ``outside``; ``format``: ``webp``, ``avif``, ``jpeg``, ``png``)."""
        return self._c.url_for(
            f"/storage/v1/files/{_core.path_of(self._c.environment)}/{_core.path_of(self.name)}/{_core.key_path(key)}",
            width=width, height=height, fit=fit, format=format, quality=quality,
        )

    def create_signed_url(self, key: str, *, expires_in: int = 300) -> Any:
        """A temporary link anyone can read the object through: ``{url, expires_at, method}``."""
        return self._sign(key, {"method": "GET", "expires_in": expires_in})

    def create_signed_upload_url(self, key: str, *, expires_in: int = 300, content_type: str | None = None, max_bytes: int | None = None) -> Any:
        """A temporary link anyone can upload one object through (a browser sending a file straight to storage)."""
        return self._sign(key, _core.compact({"method": "PUT", "expires_in": expires_in, "content_type": content_type, "max_bytes": max_bytes}))

    def _sign(self, key: str, body: Mapping[str, Any]) -> Any:
        return self._c._call("POST", f"/storage/v1/sign/{_core.path_of(self.name)}/{_core.key_path(key)}", json=dict(body), retry=True)

    def upload_to_signed_url(self, url: str, body: Body, *, content_type: str | None = None) -> Any:
        """Send *body* through a link from :meth:`create_signed_upload_url` (no key is sent)."""
        data, guessed = _content(body)
        headers = {"content-type": content_type or guessed or "application/octet-stream"}
        if self._c.is_async:
            return self._upload_async(url, data, headers)
        response = httpx.put(url, content=data, headers=headers, timeout=self._c.timeout)
        return self._c._answer(response, False)

    async def _upload_async(self, url: str, data: Any, headers: Mapping[str, str]) -> Any:
        async with httpx.AsyncClient(timeout=self._c.timeout) as client:
            return self._c._answer(await client.put(url, content=data, headers=headers), False)

    # ── sharing (a secret key) ───────────────────────────────────────────

    def create_share_link(self, key: str, *, expires_in: int = 3600, permissions: str = "both", label: str = "") -> Any:
        """A temporary link to one object, for people who have no account: ``permissions`` is ``view``, ``download`` or ``both``. Needs a secret key."""
        if not _core.is_secret_key(self._c.api_key):
            raise ConfigError("Share links are made with a secret key.")
        return self._c.manage.buckets.create_share(self.name, key, expires_in=expires_in, permissions=permissions, label=label)

    def list_share_links(self) -> Any:
        return self._c.manage.buckets.shares(self.name)

    def revoke_share_link(self, share_id: str) -> Any:
        return self._c.manage.buckets.revoke_share(self.name, share_id)
