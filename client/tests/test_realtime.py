"""The live channel client against a small in-process server that speaks Pawabase's realtime protocol."""

import asyncio
import json

import pytest
import websockets

from pawabase import AsyncPawabase, RealtimeError

pytestmark = pytest.mark.asyncio


class Server:
    """Welcomes, acks subscribe/publish/presence/history, echoes a publish as a message, refuses the channel named ``secret``."""

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.urls: list[str] = []

    async def handle(self, socket):
        self.urls.append(socket.request.path)
        await socket.send(json.dumps({"type": "welcome", "connection_id": "c1"}))
        async for raw in socket:
            frame = json.loads(raw)
            self.frames.append(frame)
            kind, ref = frame.get("type"), frame.get("ref")
            if frame.get("channel") == "secret":
                await socket.send(json.dumps({"type": "ack", "ref": ref, "ok": False, "error": "forbidden", "reason": "no"}))
            elif kind == "history":
                await socket.send(json.dumps({"type": "history", "ref": ref, "channel": frame["channel"], "messages": [{"id": "old", "event": "e"}]}))
            elif kind == "publish":
                await socket.send(json.dumps({"type": "ack", "ref": ref, "ok": True, "id": "m1"}))
                await socket.send(json.dumps({"type": "message", "id": "m1", "channel": frame["channel"], "event": frame["event"], "payload": frame["payload"]}))
            elif kind in ("subscribe", "unsubscribe", "presence"):
                await socket.send(json.dumps({"type": "ack", "ref": ref, "ok": True}))
                if kind == "subscribe":
                    await socket.send(json.dumps({"type": "presence_state", "channel": frame["channel"], "members": [{"id": "u0", "meta": {"n": "Bo"}}]}))
                    await socket.send(json.dumps({"type": "presence_diff", "channel": frame["channel"], "joins": [{"id": "u1", "meta": {"n": "Cy"}}], "leaves": [{"id": "u0"}]}))


@pytest.fixture
async def gateway():
    server = Server()
    async with websockets.serve(server.handle, "127.0.0.1", 0) as running:
        port = running.sockets[0].getsockname()[1]
        yield server, f"http://127.0.0.1:{port}"


async def test_subscribe_send_receive_presence_and_history(gateway):
    server, url = gateway
    async with AsyncPawabase(url, "pb_pk_test", environment="dev") as pb:
        room = pb.realtime.channel("chat:lobby", presence={"name": "Ada"})
        heard, joins = [], []
        room.on("say", lambda m: heard.append(m["payload"]))
        room.on_presence(lambda j, l: joins.append(([m["id"] for m in j], [m["id"] for m in l])))
        await room.subscribe()
        assert (await room.send("say", {"text": "hi"}))["id"] == "m1"
        await asyncio.sleep(0.1)
        assert heard == [{"text": "hi"}] and joins == [(["u1"], ["u0"])]
        assert [m["id"] for m in room.presence_state()] == ["u1"]
        assert [m["id"] for m in await room.history(5)] == ["old"]
        assert server.frames[0] == {"type": "subscribe", "channel": "chat:lobby", "presence": {"name": "Ada"}, "ref": "1"}
        assert "apikey=pb_pk_test" in server.urls[0] and "environment=dev" in server.urls[0]


async def test_a_refused_channel_raises_with_the_servers_reason(gateway):
    _, url = gateway
    async with AsyncPawabase(url, "pb_pk_test") as pb:
        with pytest.raises(RealtimeError) as raised:
            await pb.realtime.channel("secret").subscribe()
        assert raised.value.code == "forbidden"


async def test_iterating_a_channel_yields_its_messages_and_ends_on_close(gateway):
    _, url = gateway
    pb = AsyncPawabase(url, "pb_pk_test")
    room = pb.realtime.channel("c")
    await room.subscribe()
    await room.send("e", {"n": 1})
    first = await asyncio.wait_for(room.__anext__(), 2)
    assert first["payload"] == {"n": 1}
    await pb.close()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(room.__anext__(), 2)
