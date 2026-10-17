"""Realtime: channels, presence and live record changes.

Publishing, presence and history work over plain HTTP on either client. A live subscription (a WebSocket) needs the async client and the
``websockets`` package (``pip install "pawabase[realtime]"``):

    async with AsyncPawabase(url, key) as pb:
        room = pb.realtime.channel("chat:lobby", presence={"name": "Ada"})
        room.on("message", lambda m: print(m["payload"]))
        await room.subscribe()
        await room.send("message", {"text": "hello"})
        async for message in pb.realtime.channel("resource:posts"):      # a resource with realtime on: its changes, as you may read them
            print(message["event"], message["payload"]["record"])

Messages on ``resource:<name>`` channels are checked per subscriber against the resource's read policy, so you only hear about records you may read.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from . import _core
from .errors import ConfigError, RealtimeError

try:  # optional: only a live connection needs it
    import websockets
except ImportError:  # pragma: no cover - exercised where the extra is not installed
    websockets = None  # type: ignore[assignment]

#: Close codes after which reconnecting would not help (the key or the project is wrong, the server is going away for good).
FATAL_CLOSE = {1008, 4001, 4004, 4401}
HEARTBEAT_SECONDS = 25.0
ACK_TIMEOUT = 10.0

Handler = Callable[[dict[str, Any]], Any]


class Realtime:
    """``pb.realtime``: HTTP publishing and reading on any client, live channels on the async one."""

    def __init__(self, client: Any) -> None:
        self._c = client
        self._socket: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._beat: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._channels: dict[str, Channel] = {}
        self._refs = 0
        self._connecting: asyncio.Lock | None = None
        self._manual = False
        self.connection_id: str | None = None

    # ── over HTTP (both clients) ─────────────────────────────────────────

    def publish(self, channel: str, event: str, payload: Any = None) -> Any:
        """Publish a message to a channel from anywhere; the channel's ``publish`` rule applies."""
        return self._c._call("POST", "/realtime/v1/publish", json={"channel": channel, "event": event, "payload": payload})

    def presence(self, channel: str) -> Any:
        """Who is on the channel now."""
        return _core.then(self._c._call("GET", f"/realtime/v1/channels/{_core.key_path(channel)}/presence", retry=True), lambda body: body["members"])

    def history(self, channel: str, limit: int = 50) -> Any:
        """Recent messages on a channel, oldest first."""
        return _core.then(self._c._call("GET", f"/realtime/v1/channels/{_core.key_path(channel)}/history", params={"limit": limit}, retry=True), lambda body: body["messages"])

    # ── live (async client) ──────────────────────────────────────────────

    def channel(self, name: str, *, presence: dict[str, Any] | None = None, replay: int = 0) -> Channel:
        """A channel by name. ``presence`` is what others see of you; ``replay`` fetches that many recent messages after a reconnect."""
        _core.require_async(self._c, "A live channel")
        channel = self._channels.get(name)
        if channel is None:
            channel = self._channels[name] = Channel(self, name)
        channel.presence = presence if presence is not None else channel.presence
        channel.replay = replay or channel.replay
        return channel

    def _url(self) -> str:
        parts = urlsplit(self._c.url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        query = {"apikey": self._c.api_key, "environment": self._c.environment}
        token = self._c.auth.access_token
        if token:
            query["token"] = token
        return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + "/realtime/v1/socket", urlencode(query), ""))

    async def connect(self) -> None:
        """Open the connection (done for you by the first ``subscribe``)."""
        _core.require_async(self._c, "A realtime connection")
        if websockets is None:
            raise ConfigError('Live channels need the websockets package: pip install "pawabase[realtime]".')
        if self._socket is not None:
            return
        if self._connecting is None:
            self._connecting = asyncio.Lock()
        async with self._connecting:
            if self._socket is not None:
                return
            self._manual = False
            socket = await websockets.connect(self._url(), open_timeout=10, ping_interval=None, max_size=16 * 1024 * 1024)
            welcome = json.loads(await asyncio.wait_for(socket.recv(), 10))
            if welcome.get("type") != "welcome":
                await socket.close()
                raise RealtimeError("no_welcome", f"The server did not welcome the connection: {welcome}")
            self.connection_id = welcome.get("connection_id")
            self._socket = socket
            self._reader = asyncio.create_task(self._read(socket))
            self._beat = asyncio.create_task(self._heartbeat(socket))

    async def close(self) -> None:
        """Close the connection and end every channel's iteration."""
        self._manual = True
        for task in (self._reader, self._beat):
            if task is not None:
                task.cancel()
        self._reader = self._beat = None
        socket, self._socket = self._socket, None
        self._fail_pending(RealtimeError("closed", "The realtime connection was closed."))
        for channel in self._channels.values():
            channel._end("disconnected")
        if socket is not None:
            with contextlib.suppress(Exception):
                await socket.close()

    async def _heartbeat(self, socket: Any) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            with contextlib.suppress(Exception):
                await socket.send(json.dumps({"type": "ping"}))

    async def _read(self, socket: Any) -> None:
        code = 1006
        try:
            async for raw in socket:
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue
                self._on_frame(frame)
            code = getattr(socket, "close_code", 1006) or 1006
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a dropped connection is handled below, not raised from a task
            code = 1006
        if self._socket is socket:
            self._socket = None
        self._fail_pending(RealtimeError("closed", "The realtime connection closed."))
        if self._manual or code in FATAL_CLOSE:
            for channel in self._channels.values():
                channel._end(f"closed ({code})")
            return
        asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        delay = 0.5
        while not self._manual:
            try:
                await self.connect()
                for channel in list(self._channels.values()):
                    if channel._wanted:
                        await channel._join(rejoining=True)
                return
            except Exception:  # noqa: BLE001 - keep trying with a growing pause
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15.0)

    def _on_frame(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind in ("ack", "history"):
            future = self._pending.pop(str(frame.get("ref")), None)
            if future is None or future.done():
                return
            if kind == "ack" and frame.get("ok") is False:
                future.set_exception(RealtimeError(str(frame.get("error") or "error"), str(frame.get("reason") or frame.get("error") or "The request was refused.")))
            else:
                future.set_result(frame)
        elif kind == "message":
            channel = self._channels.get(str(frame.get("channel")))
            if channel:
                channel._deliver(frame)
        elif kind == "presence_state":
            channel = self._channels.get(str(frame.get("channel")))
            if channel:
                channel._members = {str(m.get("id") or m.get("connection_id") or i): m for i, m in enumerate(frame.get("members") or [])}
        elif kind == "presence_diff":
            channel = self._channels.get(str(frame.get("channel")))
            if channel:
                channel._presence_diff(frame.get("joins") or [], frame.get("leaves") or [])

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def request(self, frame: dict[str, Any]) -> dict[str, Any]:
        await self.connect()
        self._refs += 1
        ref = str(self._refs)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[ref] = future
        try:
            await self._socket.send(json.dumps({**frame, "ref": ref}))
            return await asyncio.wait_for(future, ACK_TIMEOUT)
        except asyncio.TimeoutError as error:
            self._pending.pop(ref, None)
            raise RealtimeError("ack_timeout", f"The server did not answer within {ACK_TIMEOUT:g} s.") from error


class Channel:
    """One channel: handlers by event, ``send``, presence, history, and async iteration over its messages."""

    def __init__(self, realtime: Realtime, name: str) -> None:
        self._rt = realtime
        self.name = name
        self.presence: dict[str, Any] | None = None
        self.replay = 0
        self._handlers: dict[str | None, list[Handler]] = {}
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._members: dict[str, dict[str, Any]] = {}
        self._seen: dict[str, None] = {}
        self._wanted = False
        self.subscribed = False

    def on(self, event: str | None, handler: Handler) -> Channel:
        """Call ``handler(message)`` for messages with this ``event`` (``None``: every message). Chainable."""
        self._handlers.setdefault(event, []).append(handler)
        return self

    def on_presence(self, handler: Callable[[list[dict[str, Any]], list[dict[str, Any]]], Any]) -> Channel:
        """Call ``handler(joins, leaves)`` when people come and go."""
        self._handlers.setdefault("__presence__", []).append(handler)  # type: ignore[arg-type]
        return self

    async def subscribe(self) -> Channel:
        """Start receiving. Refused with :class:`~pawabase.errors.RealtimeError` when the channel's rules say so."""
        self._wanted = True
        await self._join(rejoining=False)
        return self

    async def _join(self, *, rejoining: bool) -> None:
        await self._rt.request({"type": "subscribe", "channel": self.name, **({"presence": self.presence} if self.presence else {})})
        self.subscribed = True
        if rejoining and self.replay:
            for message in await self.history(self.replay):
                self._deliver(message)

    async def unsubscribe(self) -> None:
        self._wanted = False
        if self.subscribed:
            with contextlib.suppress(Exception):
                await self._rt.request({"type": "unsubscribe", "channel": self.name})
        self.subscribed = False
        self._members.clear()
        self._end("unsubscribed")
        self._rt._channels.pop(self.name, None)

    async def send(self, event: str, payload: Any = None) -> dict[str, Any]:
        """Publish over the socket; resolves with ``{"id": …}`` once the server accepted it."""
        ack = await self._rt.request({"type": "publish", "channel": self.name, "event": event, "payload": payload})
        return {"id": ack.get("id")}

    async def track(self, meta: dict[str, Any]) -> None:
        """Set what others see of you here. Subscribe first."""
        self.presence = meta
        await self._rt.request({"type": "presence", "channel": self.name, "meta": meta})

    async def history(self, limit: int = 50) -> list[dict[str, Any]]:
        reply = await self._rt.request({"type": "history", "channel": self.name, "limit": limit})
        return list(reply.get("messages") or [])

    def presence_state(self) -> list[dict[str, Any]]:
        """Who is here, as last told by the server (empty unless presence is on for the channel)."""
        return list(self._members.values())

    # ── delivery ─────────────────────────────────────────────────────────

    def _deliver(self, message: dict[str, Any]) -> None:
        ident = str(message.get("id") or "")
        if ident:
            if ident in self._seen:
                return  # a replay after a reconnect repeats messages already seen
            self._seen[ident] = None
            if len(self._seen) > 500:
                self._seen.pop(next(iter(self._seen)))
        for handler in [*self._handlers.get(message.get("event"), []), *self._handlers.get(None, [])]:
            result = handler(message)
            if inspect.isawaitable(result):
                asyncio.ensure_future(result)
        self._queue.put_nowait(message)

    def _presence_diff(self, joins: list[dict[str, Any]], leaves: list[dict[str, Any]]) -> None:
        for member in joins:
            self._members[str(member.get("id") or member.get("connection_id") or len(self._members))] = member
        for member in leaves:
            self._members.pop(str(member.get("id") or member.get("connection_id")), None)
        for handler in self._handlers.get("__presence__", []):
            result = handler(joins, leaves)  # type: ignore[call-arg]
            if inspect.isawaitable(result):
                asyncio.ensure_future(result)

    def _end(self, reason: str) -> None:
        self.subscribed = False
        self._queue.put_nowait(None)

    def __aiter__(self) -> Channel:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._wanted:
            await self.subscribe()
        message = await self._queue.get()
        if message is None:
            raise StopAsyncIteration
        return message
