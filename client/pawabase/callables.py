"""Flows and custom routes you have defined, called by name.

    pb.flows.invoke("welcome", email="ada@example.com")  # a flow: its response
    pb.routes.post("orders/42/pay", method="card")       # a custom route under /rest/v1
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import _core


class Flows:
    def __init__(self, client: Any) -> None:
        self._c = client

    def invoke(self, name: str, *, headers: Mapping[str, str] | None = None, **input: Any) -> Any:
        """Run a flow that has a direct HTTP trigger (no path) under that trigger's policy; its response is returned."""
        return self._c._call("POST", f"/flows/v1/{_core.key_path(name)}", json=input or None, headers=headers)

    run = invoke


class Routes:
    """Your own HTTP routes (``routes`` bound to flows), served under ``/rest/v1``."""

    def __init__(self, client: Any) -> None:
        self._c = client

    def call(self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None) -> Any:
        return self._c._call(method.upper(), f"/rest/v1/{_core.key_path(path)}", json=json, params=params, headers=headers, retry=method.upper() in ("GET", "HEAD"))

    def get(self, path: str, **params: Any) -> Any:
        return self.call("GET", path, params=params or None)

    def post(self, path: str, *, params: Mapping[str, Any] | None = None, **body: Any) -> Any:
        return self.call("POST", path, json=body or None, params=params)

    def put(self, path: str, *, params: Mapping[str, Any] | None = None, **body: Any) -> Any:
        return self.call("PUT", path, json=body or None, params=params)

    def patch(self, path: str, *, params: Mapping[str, Any] | None = None, **body: Any) -> Any:
        return self.call("PATCH", path, json=body or None, params=params)

    def delete(self, path: str, **params: Any) -> Any:
        return self.call("DELETE", path, params=params or None)
