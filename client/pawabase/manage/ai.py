"""``pb.manage.ai``: the AI gateway of the environment.

Connect providers (OpenAI, Anthropic, Gemini, Mistral, Groq, or any OpenAI-compatible server), expose models by name, try them, and read what
they cost. Provider keys are sealed on arrival and never returned.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .. import _core
from ._base import Service, seg


class Ai(Service):
    def path(self, suffix: str = "") -> str:
        return super().path(f"/ai{suffix}")

    def catalog(self) -> Any:
        """The kinds of provider that can be connected, with suggested models."""
        return self.get("/catalog")

    def providers(self) -> Any:
        return self.get("/providers")

    def provider_models(self, name: str) -> Any:
        """The models a connected provider offers this key, asked live: ``{"data": [{"id", "kind", "label"}], "source": "provider" | "catalog", "error"}``."""
        return self.get(seg("providers", name, "models"))

    def connect(self, *, name: str, kind: str, key: str = "", base_url: str = "") -> Any:
        """Connect a provider. ``kind``: ``openai``, ``anthropic``, ``gemini``, ``mistral``, ``groq`` or ``openai_compatible`` (needs ``base_url``)."""
        return self.post("/providers", {"name": name, "kind": kind, "key": key, "base_url": base_url})

    def update_provider(self, name: str, *, key: str | None = None, base_url: str | None = None, enabled: bool | None = None) -> Any:
        return self.patch(seg("providers", name), _core.compact({"key": key, "base_url": base_url, "enabled": enabled}))

    def disconnect(self, name: str) -> Any:
        """Disconnect a provider and every model on it."""
        return self.delete(seg("providers", name))

    def models(self) -> Any:
        return self.get("/models")

    def add_model(
        self, *, alias: str, provider: str, upstream: str, kind: str = "chat", description: str = "", policy: Any = "service",
        params: Mapping[str, Any] | None = None, dimensions: int | None = None, input_price: float | None = None, output_price: float | None = None,
        fallback: str | None = None, enabled: bool = True,
    ) -> Any:
        """Expose a model under ``alias``. ``policy`` is who may call it: ``service`` (servers only, the default), ``authenticated``, ``public`` or a named policy.

        ``params`` are defaults such as ``system`` and ``temperature``; prices are USD per million tokens.
        """
        body = {"alias": alias, "provider": provider, "upstream": upstream, "kind": kind, "description": description, "policy": policy,
                "params": dict(params or {}), "enabled": enabled}
        body.update(_core.compact({"dimensions": dimensions, "input_price": input_price, "output_price": output_price, "fallback": fallback}))
        return self.post("/models", body)

    def update_model(self, alias: str, **changes: Any) -> Any:
        """Change what is given: ``provider``, ``upstream``, ``description``, ``policy``, ``params``, ``dimensions``, ``input_price``, ``output_price``, ``fallback``, ``enabled``."""
        return self.patch(seg("models", alias), dict(changes))

    def remove_model(self, alias: str) -> Any:
        return self.delete(seg("models", alias))

    def try_model(self, alias: str, *, messages: Sequence[Mapping[str, Any]] | None = None, input: Sequence[str] | None = None, max_tokens: int | None = None) -> Any:
        """Send one request to a model, as Studio's playground does: ``{"text", "usage", "ms"}``, or ``{"error"}``."""
        return self.post(seg("models", alias, "try"), _core.compact({"messages": [dict(m) for m in messages] if messages else None, "input": list(input) if input else None, "max_tokens": max_tokens}))

    def usage(self, *, days: int = 7) -> Any:
        """Calls, tokens, cost and latency over ``days`` (1 to 90), in all and per model."""
        return self.get("/usage", days=days)

    def requests(self, *, alias: str | None = None, failed: bool = False, limit: int = 50) -> Any:
        """Recent calls. Never includes what was asked or answered."""
        return self.get("/requests", alias=alias, failed="true" if failed else None, limit=limit)

    def vectors(self) -> Any:
        """Vector fields in the environment, and how many records have no vector yet."""
        return self.get("/vectors")

    def backfill(self, resource: str, *, limit: int = 100) -> Any:
        """Embed up to ``limit`` records of a resource that have no vector. Call again while ``embedded`` is above zero."""
        return self.post(seg("backfill", resource), None, limit=limit)

    def search(self, resource: str, *, text: str | None = None, vector: Sequence[float] | None = None, field: str | None = None, metric: str = "cosine", limit: int = 10) -> Any:
        """Search a resource by meaning, as Studio does: with ``text`` or a ``vector``."""
        return self.post(seg("search", resource), _core.compact({"text": text, "vector": list(vector) if vector is not None else None, "field": field, "metric": metric, "limit": limit}))
