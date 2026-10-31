"""``pb.ai``: the AI gateway. Chat and embedding models your environment has connected, behind one OpenAI-compatible API.

    answer = pb.ai.chat("chat", "Summarise this ticket: …")
    for piece in pb.ai.stream("chat", "Tell me a story"):
        print(piece, end="")
    vector = pb.ai.embed("embed", "hello")

Models are called by the name your environment gave them. Who may call each is the model's policy; servers (a secret key) always may. The
async client has the same methods, awaited; ``stream`` is then an async generator.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from typing import Any

from . import _core
from .errors import PawabaseError

Messages = "str | Sequence[Mapping[str, Any]]"


def _messages(input: str | Sequence[Mapping[str, Any]]) -> list[Any]:
    return [{"role": "user", "content": input}] if isinstance(input, str) else [dict(m) for m in input]


def _body(model: str, input: str | Sequence[Mapping[str, Any]], options: Mapping[str, Any]) -> dict[str, Any]:
    names = {"temperature": "temperature", "top_p": "top_p", "max_tokens": "max_tokens", "stop": "stop", "tools": "tools", "tool_choice": "tool_choice", "response_format": "response_format"}
    body: dict[str, Any] = {"model": model, "messages": _messages(input)}
    for key, value in options.items():
        if value is None:
            continue
        body[names.get(key, key)] = value
    return body


def _piece(line: str) -> str | None:
    """The text in one server-sent line, ``None`` for a line that carries none, ``""`` at the end. Raises when the stream reports an error."""
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if data == "[DONE]":
        return ""
    try:
        parsed = json.loads(data)
    except ValueError:
        return None
    if isinstance(parsed, dict) and parsed.get("error"):
        raise PawabaseError(502, {"error": parsed["error"]})
    choices = parsed.get("choices") or []
    return (choices[0].get("delta") or {}).get("content") if choices else None


class Ai:
    def __init__(self, client: Any) -> None:
        self._c = client

    def models(self) -> Any:
        """The models the caller may use: ``[{"id": "chat", "kind": "chat", …}]``."""
        return _core.then(self._c._call("GET", "/ai/v1/models", retry=True), lambda body: body["data"])

    def complete(self, model: str, input: str | Sequence[Mapping[str, Any]], **options: Any) -> Any:
        """The full chat completion in OpenAI's shape (tool calls, usage and all). ``options``: ``temperature``, ``max_tokens``, ``tools``…"""
        return self._c._call("POST", "/ai/v1/chat/completions", json=_body(model, input, options))

    def chat(self, model: str, input: str | Sequence[Mapping[str, Any]], **options: Any) -> Any:
        """Ask a model and get its answer as text."""
        return _core.then(self.complete(model, input, **options), lambda r: ((r.get("choices") or [{}])[0].get("message") or {}).get("content") or "")

    def embed(self, model: str, input: str | Sequence[str]) -> Any:
        """The vector for one text, or a list of vectors (in order) for a list of texts."""
        single = isinstance(input, str)

        def vectors(body: Mapping[str, Any]) -> Any:
            rows = [row["embedding"] for row in sorted(body["data"], key=lambda r: r.get("index", 0))]
            return (rows[0] if rows else []) if single else rows

        return _core.then(self._c._call("POST", "/ai/v1/embeddings", json={"model": model, "input": input if single else list(input)}), vectors)

    def stream(self, model: str, input: str | Sequence[Mapping[str, Any]], **options: Any) -> Any:
        """The answer as it is written: an iterator of text pieces (an async iterator on the async client)."""
        body = {**_body(model, input, options), "stream": True}
        if inspect.iscoroutinefunction(getattr(self._c, "_call", None)):
            return self._astream(body)
        return self._sstream(body)

    def _sstream(self, body: dict[str, Any]) -> Iterator[str]:
        with self._c.stream("POST", "/ai/v1/chat/completions", json=body) as response:
            if response.status_code >= 400:
                response.read()
                raise _fail(response)
            for line in response.iter_lines():
                piece = _piece(line)
                if piece == "":
                    return
                if piece:
                    yield piece

    async def _astream(self, body: dict[str, Any]) -> AsyncIterator[str]:
        async with self._c.stream("POST", "/ai/v1/chat/completions", json=body) as response:
            if response.status_code >= 400:
                await response.aread()
                raise _fail(response)
            async for line in response.aiter_lines():
                piece = _piece(line)
                if piece == "":
                    return
                if piece:
                    yield piece


def _fail(response: Any) -> PawabaseError:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    return PawabaseError(response.status_code, detail, request_id=response.headers.get("x-request-id"))
