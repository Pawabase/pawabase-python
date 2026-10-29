"""Storage administration: buckets, the objects in them, temporary share links and signed URLs.

(Reading and writing files as your app does is ``pb.storage``; this is the operator's view and works with a secret key regardless of policies.)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._base import Definitions, Service, seg


class Buckets(Definitions):
    """Buckets: a name, public or private, accepted types, size limit, per-action policies, signed URLs, image transforms and caching."""

    kind = "buckets"

    def objects(self, bucket: str, *, prefix: str | None = None, cursor: str | None = None, limit: int | None = None) -> Any:
        """One page of the bucket's objects: ``{files, prefixes, cursor}``."""
        return self.get(seg(self.kind, bucket, "objects"), prefix=prefix, cursor=cursor, limit=limit)

    def object(self, bucket: str, key: str) -> Any:
        """One object's details and a way to read it."""
        return self.get(seg(self.kind, bucket, "object"), key=key)

    def put_object(self, bucket: str, key: str, content: bytes | str, content_type: str = "application/octet-stream") -> Any:
        data = content.encode() if isinstance(content, str) else content
        return self._c._call("PUT", self.path(seg(self.kind, bucket, "object")), params={"key": key}, content=data, headers={"content-type": content_type})

    def delete_object(self, bucket: str, key: str) -> Any:
        return self.delete(seg(self.kind, bucket, "objects", key))

    def move_object(self, bucket: str, source: str, target: str) -> Any:
        """Rename or move an object inside the bucket."""
        return self.post(seg(self.kind, bucket, "object", "move"), {"source": source, "target": target})

    def sign(self, bucket: str, key: str, *, method: str = "GET", expires_in: int = 300, content_type: str | None = None, max_bytes: int | None = None) -> Any:
        """A temporary signed link to one object (``GET`` to read, ``PUT`` to upload)."""
        body = {"key": key, "method": method, "expires_in": expires_in, "content_type": content_type, "max_bytes": max_bytes}
        return self.post(seg(self.kind, bucket, "sign"), {k: v for k, v in body.items() if v is not None})

    # share links
    def shares(self, bucket: str) -> Any:
        return self.get(seg(self.kind, bucket, "shares"))

    def create_share(self, bucket: str, key: str, *, expires_in: int = 3600, permissions: str = "both", label: str = "") -> Any:
        """A link to one object for people with no account. ``permissions``: ``view``, ``download``, ``both``. The URL is in the answer."""
        return self.post(seg(self.kind, bucket, "shares"), {"key": key, "expires_in": expires_in, "permissions": permissions, "label": label})

    def revoke_share(self, bucket: str, share_id: str) -> Any:
        return self.delete(seg(self.kind, bucket, "shares", share_id))


class Cache(Service):
    """The environment's response cache."""

    def stats(self) -> Any:
        return self.get("/cache")

    def invalidate(self, *, tags: list[str] | None = None, resources: list[str] | None = None) -> Any:
        """Drop cached responses by tag (``resource:posts``) or for whole resources."""
        return self.post("/cache/invalidate", {"tags": tags or [], "resources": resources or []})
