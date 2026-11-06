"""``pb.ai``: the AI gateway. Chat and embedding models your environment has connected, behind one OpenAI-compatible API.

    answer = pb.ai.chat("chat", "Summarise this ticket: …")
    for piece in pb.ai.stream("chat", "Tell me a story"):
        print(piece, end="")
    vector = pb.ai.embed("embed", "hello")

Models are called by the name your environment gave them. Who may call each is the model's policy; servers (a secret key) always may. The
async client has the same methods, awaited; ``stream`` is then an async generator.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
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


def _seg(*parts: Any) -> str:
    return "/ai/v1/" + _core.path_of(*parts)


def _event(line: str) -> dict[str, Any] | None:
    """The event in one server-sent line: ``None`` for a line that carries none, ``{"type": "done"}`` at the end. Raises when the run reports an error."""
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if data == "[DONE]":
        return {"type": "done"}
    try:
        parsed = json.loads(data)
    except ValueError:
        return None
    if isinstance(parsed, dict) and parsed.get("type") == "error":
        raise PawabaseError(502, {"error": {"message": str(parsed.get("message") or "the run stopped"), "code": "agent_error"}})
    return parsed if isinstance(parsed, dict) else None


class Agents:
    """``pb.ai.agents``: agents the caller may run. Who may run one is the agent's access policy."""

    def __init__(self, client: Any) -> None:
        self._c = client

    def list(self) -> Any:
        """The agents the caller may run: ``[{"name", "description"}]``."""
        return _core.then(self._c._call("GET", "/ai/v1/agents", retry=True), lambda body: body["data"])

    def run(self, name: str, input: str, *, thread: str | None = None, background: bool = False) -> Any:
        """Send *input* and wait for the run (or, with *background*, only for it to start). Returns the run: ``id``, ``status``, ``output``…"""
        body: dict[str, Any] = {"input": input, "background": background}
        if thread:
            body["thread"] = thread
        return self._c._call("POST", _seg("agents", name, "runs"), json=body)

    def get(self, run_id: str) -> Any:
        """A run of yours: its status and answer, and what it is waiting on if it stopped to ask."""
        return self._c._call("GET", _seg("agent-runs", run_id), retry=True)

    def decide(self, run_id: str, *, decisions: Mapping[str, bool] | None = None, approve: bool | None = None, background: bool = False) -> Any:
        """Approve or decline what a run is waiting on: a decision per call id, and *approve* for any not named."""
        body: dict[str, Any] = {"decisions": dict(decisions or {}), "background": background}
        if approve is not None:
            body["approve"] = approve
        return self._c._call("POST", _seg("agent-runs", run_id, "decide"), json=body)

    def wait(self, run_id: str, *, interval: float = 1.0, timeout: float = 300.0) -> Any:
        """Poll a background run until it is no longer ``running`` (a ``waiting`` run is returned too, so you can decide it)."""
        if inspect.iscoroutinefunction(getattr(self._c, "_call", None)):
            return self._await(run_id, interval, timeout)
        deadline = time.monotonic() + timeout
        while True:
            run = self.get(run_id)
            if run.get("status") != "running":
                return run
            if time.monotonic() >= deadline:
                raise TimeoutError(f"run {run_id} was still running after {timeout} seconds")
            time.sleep(interval)

    async def _await(self, run_id: str, interval: float, timeout: float) -> Any:
        deadline = time.monotonic() + timeout
        while True:
            run = await self.get(run_id)
            if run.get("status") != "running":
                return run
            if time.monotonic() >= deadline:
                raise TimeoutError(f"run {run_id} was still running after {timeout} seconds")
            await asyncio.sleep(interval)

    def stream(self, name: str, input: str, *, thread: str | None = None) -> Any:
        """Each step of a run as it happens: an iterator of event dicts (an async iterator on the async client)."""
        body: dict[str, Any] = {"input": input, "stream": True}
        if thread:
            body["thread"] = thread
        if inspect.iscoroutinefunction(getattr(self._c, "_call", None)):
            return self._astream(body, name)
        return self._sstream(body, name)

    def _sstream(self, body: dict[str, Any], name: str) -> Iterator[dict[str, Any]]:
        with self._c.stream("POST", _seg("agents", name, "runs"), json=body) as response:
            if response.status_code >= 400:
                response.read()
                raise _fail(response)
            for line in response.iter_lines():
                event = _event(line)
                if event is None:
                    continue
                if event.get("type") == "done":
                    return
                yield event

    async def _astream(self, body: dict[str, Any], name: str) -> AsyncIterator[dict[str, Any]]:
        async with self._c.stream("POST", _seg("agents", name, "runs"), json=body) as response:
            if response.status_code >= 400:
                await response.aread()
                raise _fail(response)
            async for line in response.aiter_lines():
                event = _event(line)
                if event is None:
                    continue
                if event.get("type") == "done":
                    return
                yield event


class Knowledge:
    """``pb.ai.knowledge``: knowledge bases the caller may search or add documents to."""

    def __init__(self, client: Any) -> None:
        self._c = client

    def search(self, name: str, query: str, *, limit: int = 5) -> Any:
        """By meaning and by words together. Documents kept private to someone else are left out."""
        return _core.then(self._c._call("POST", _seg("knowledge", name, "search"), json={"query": query, "limit": limit}), lambda body: body["data"])

    def add_document(self, name: str, *, text: str | None = None, url: str | None = None, title: str = "", source: str = "") -> Any:
        """Add text, or a web page to read (*url*). A signed-in user's documents are found only by them. The same *source* again replaces it."""
        body: dict[str, Any] = {"title": title, "source": source}
        if text is not None:
            body["text"] = text
        if url is not None:
            body["url"] = url
        return self._c._call("POST", _seg("knowledge", name, "documents"), json=body)

    def add_file(self, name: str, file: Any, *, filename: str = "document", title: str = "", source: str = "") -> Any:
        """Add a file (PDF, Word, HTML or text): bytes or an open binary file."""
        return self._c._call(
            "POST", _seg("knowledge", name, "documents"), files={"file": (filename, file)}, data={k: v for k, v in {"title": title, "source": source}.items() if v}
        )

    def document(self, name: str, document_id: str) -> Any:
        """Whether a document is ready to search, and why not if it failed."""
        return self._c._call("GET", _seg("knowledge", name, "documents", document_id), retry=True)


class Ai:
    def __init__(self, client: Any) -> None:
        self._c = client
        self.agents = Agents(client)
        self.knowledge = Knowledge(client)

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
