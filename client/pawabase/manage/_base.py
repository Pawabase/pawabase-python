"""What every management service shares: the environment a call is for, and the five verbs.

Everything under ``pb.manage`` is the management plane of **one environment**, reached with that environment's secret key (``pb_sk_…``). The
environment is the client's (``environment=`` when it was made); ``pb.manage.environment("staging")`` is the same API for another one the key may manage.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .. import _core


class Service:
    def __init__(self, client: Any, environment: str | None = None) -> None:
        self._c = client
        self._environment = environment

    @property
    def env(self) -> str:
        return self._environment or self._c.environment

    def path(self, suffix: str = "") -> str:
        return f"/platform/v1/envs/{_core.path_of(self.env)}{suffix}"

    def get(self, suffix: str = "", **params: Any) -> Any:
        return self._c._call("GET", self.path(suffix), params=_core.compact(params) or None, retry=True)

    def post(self, suffix: str = "", body: Any = None, **params: Any) -> Any:
        return self._c._call("POST", self.path(suffix), json=body, params=_core.compact(params) or None)

    def put(self, suffix: str, body: Any = None, **params: Any) -> Any:
        return self._c._call("PUT", self.path(suffix), json=body, params=_core.compact(params) or None)

    def patch(self, suffix: str, body: Any = None, **params: Any) -> Any:
        return self._c._call("PATCH", self.path(suffix), json=body, params=_core.compact(params) or None)

    def delete(self, suffix: str, **params: Any) -> Any:
        return self._c._call("DELETE", self.path(suffix), params=_core.compact(params) or None)


def seg(*parts: Any) -> str:
    """URL path segments, each quoted: ``seg("flows", name, "run")`` → ``/flows/<name>/run``."""
    return "/" + _core.path_of(*parts)


class Definitions(Service):
    """A kind of definition an environment holds (``flows``, ``policies``, ``webhooks``…): list, get, create, replace, delete.

    ``branch`` works on a feature branch's draft instead of the live environment (see ``pb.manage.releases``). A definition is addressed by its
    name (inbound hooks: their slug; routes: their id).
    """

    kind = ""

    def list(self, *, branch: str | None = None) -> Any:
        return self.get(seg(self.kind), branch=branch)

    def get_one(self, key: str, *, branch: str | None = None) -> Any:
        return self.get(seg(self.kind, key), branch=branch)

    def create(self, body: Mapping[str, Any], *, branch: str | None = None) -> Any:
        return self.post(seg(self.kind), dict(body), branch=branch)

    def replace(self, key: str, body: Mapping[str, Any], *, branch: str | None = None) -> Any:
        return self.put(seg(self.kind, key), dict(body), branch=branch)

    def remove(self, key: str, *, branch: str | None = None) -> Any:
        return self.delete(seg(self.kind, key), branch=branch)
