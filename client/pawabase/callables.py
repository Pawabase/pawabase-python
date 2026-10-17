"""Functions, flows and custom routes you have defined, called by name.

    pb.functions.invoke("orders.create", {"sku": "A1"})      # a function: its return value
    pb.flows.invoke("welcome", {"email": "ada@example.com"})  # a flow: its response
    pb.routes.post("orders/42/pay", {"method": "card"})       # a custom route under /rest/v1
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import _core


class Functions:
    def __init__(self, client: Any) -> None:
        self._c = client

    def invoke(self, name: str, input: Any = None, *, branch: str | None = None, headers: Mapping[str, str] | None = None) -> Any:
        """Run a deployed function under its own policy and return what it returned. ``branch`` runs the version deployed on a feature branch."""
        call = self._c._call(
            "POST", f"/functions/v1/{_core.key_path(name)}", json=input, params={"branch": branch} if branch and branch != "main" else None, headers=headers
        )
        return _core.then(call, lambda body: body.get("data") if isinstance(body, dict) and "data" in body else body)

    call = invoke

    def request(
        self,
        name: str,
        path: str = "/",
        *,
        method: str | None = None,
        json: Any = None,
        content: bytes | str | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        branch: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Call an HTTP function (``functions/<name>/index.ts``) with any method and path; returns the ``httpx.Response`` as it is.

        An error status is returned, not raised: an HTTP function chooses its own statuses. ``method`` defaults to ``POST`` when a body
        is given and ``GET`` otherwise. ``branch`` runs the version deployed on a feature branch.

            r = pb.functions.request("orders", "/42/items", method="POST", json={"sku": "A1"})
            if r.status_code == 201: print(r.json())
        """
        verb = (method or ("POST" if json is not None or content is not None else "GET")).upper()
        rest = "" if path in ("", "/") else "/" + path.lstrip("/")
        extra = {**({"x-pawabase-branch": branch} if branch and branch != "main" else {}), **dict(headers or {})}
        return self._c._call(
            verb,
            f"/functions/v1/{_core.key_path(name)}{rest}",
            json=json,
            content=content.encode() if isinstance(content, str) else content,
            params=params,
            headers=extra or None,
            retry=verb in ("GET", "HEAD"),
            raw="any",
            timeout=timeout,
        )

    fetch = request


class Flows:
    def __init__(self, client: Any) -> None:
        self._c = client

    def invoke(self, name: str, input: Any = None, *, headers: Mapping[str, str] | None = None) -> Any:
        """Run a flow that has a direct HTTP trigger (no path) under that trigger's policy; its response is returned."""
        return self._c._call("POST", f"/flows/v1/{_core.key_path(name)}", json=input, headers=headers)

    run = invoke


class Routes:
    """Your own HTTP routes (``routes`` bound to flows), served under ``/rest/v1``."""

    def __init__(self, client: Any) -> None:
        self._c = client

    def call(self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None) -> Any:
        return self._c._call(method.upper(), f"/rest/v1/{_core.key_path(path)}", json=json, params=params, headers=headers, retry=method.upper() in ("GET", "HEAD"))

    def get(self, path: str, **params: Any) -> Any:
        return self.call("GET", path, params=params or None)

    def post(self, path: str, json: Any = None, **params: Any) -> Any:
        return self.call("POST", path, json=json, params=params or None)

    def put(self, path: str, json: Any = None, **params: Any) -> Any:
        return self.call("PUT", path, json=json, params=params or None)

    def patch(self, path: str, json: Any = None, **params: Any) -> Any:
        return self.call("PATCH", path, json=json, params=params or None)

    def delete(self, path: str, **params: Any) -> Any:
        return self.call("DELETE", path, params=params or None)
