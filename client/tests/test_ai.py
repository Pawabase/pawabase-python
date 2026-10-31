"""``pb.ai``, ``resource.similar`` and ``pb.manage.ai``: the requests they make and the answers they unwrap."""

import json

import httpx
import pytest

from pawabase import PawabaseError
from tests.conftest import make, make_async

BASE = "/platform/v1/envs/production"


def completion(text="hello"):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}], "usage": {"total_tokens": 4}})


def test_chat_returns_the_text_and_sends_the_options(wire, pb):
    wire.answer(completion())
    assert pb.ai.chat("chat", "hi", temperature=0.2, max_tokens=20) == "hello"
    assert wire.last.url.path == "/ai/v1/chat/completions"
    assert wire.json() == {"model": "chat", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.2, "max_tokens": 20}
    full = pb.ai.complete("chat", [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}])
    assert full["usage"]["total_tokens"] == 4 and len(wire.json()["messages"]) == 2


def test_embed_returns_a_vector_for_a_string_and_vectors_in_order_for_a_list(wire, pb):
    wire.answer(httpx.Response(200, json={"data": [{"index": 1, "embedding": [3, 4]}, {"index": 0, "embedding": [1, 2]}]}))
    assert pb.ai.embed("embed", ["a", "b"]) == [[1, 2], [3, 4]]
    assert wire.json() == {"model": "embed", "input": ["a", "b"]}
    wire.answer(httpx.Response(200, json={"data": [{"index": 0, "embedding": [9, 9]}]}))
    assert pb.ai.embed("embed", "a") == [9, 9]


def test_a_stream_yields_text_pieces_until_done(wire, pb):
    events = ['{"choices":[{"delta":{"content":"Hel"}}]}', '{"choices":[{"delta":{"content":"lo"}}]}', "[DONE]"]
    wire.answer(httpx.Response(200, content="".join(f"data: {e}\n\n" for e in events).encode(), headers={"content-type": "text/event-stream"}))
    assert list(pb.ai.stream("chat", "hi")) == ["Hel", "lo"]
    assert wire.json()["stream"] is True


def test_a_gateway_error_keeps_its_message_and_code(wire, pb):
    wire.answer(httpx.Response(403, json={"error": {"message": "you may not use this model", "type": "invalid_request_error", "code": "forbidden"}}))
    with pytest.raises(PawabaseError) as caught:
        pb.ai.chat("chat", "hi")
    assert caught.value.status_code == 403 and caught.value.code == "forbidden" and caught.value.message == "you may not use this model"


def test_similar_posts_a_text_or_vector_query(wire, pb):
    wire.answer(httpx.Response(200, json={"data": [{"id": "1", "distance": 0.1}], "metric": "cosine", "count": 1}))
    found = pb.from_("notes").similar(text="cats", limit=3, max_distance=0.5)
    assert wire.last.url.path == "/rest/v1/notes/similar"
    assert wire.json() == {"text": "cats", "metric": "cosine", "limit": 3, "max_distance": 0.5}
    assert found["data"][0]["distance"] == 0.1


async def test_the_async_client_has_the_same_methods(wire):
    wire.answer(completion("hi there"))
    pb = make_async(wire)
    assert await pb.ai.chat("chat", "hi") == "hi there"
    events = ['{"choices":[{"delta":{"content":"a"}}]}', "[DONE]"]
    wire.answer(httpx.Response(200, content="".join(f"data: {e}\n\n" for e in events).encode()))
    assert [piece async for piece in pb.ai.stream("chat", "hi")] == ["a"]


def test_gateway_administration_is_scoped_to_the_environment(wire):
    wire.answer(httpx.Response(200, json={"data": []}))
    sk = make(wire, key="pb_sk_x", environment="production")
    ai = sk.manage.ai
    ai.connect(name="openai", kind="openai", key="sk-1")
    assert wire.last.url.path == f"{BASE}/ai/providers" and wire.json() == {"name": "openai", "kind": "openai", "key": "sk-1", "base_url": ""}
    ai.add_model(alias="chat", provider="openai", upstream="gpt-4o-mini", input_price=0.15, params={"system": "Be brief."})
    assert wire.last.url.path == f"{BASE}/ai/models" and wire.json()["policy"] == "service" and wire.json()["input_price"] == 0.15
    ai.provider_models("openai")
    assert wire.last.url.path == f"{BASE}/ai/providers/openai/models"
    ai.usage(days=30)
    assert wire.last.url.path == f"{BASE}/ai/usage" and wire.query() == {"days": "30"}
    ai.backfill("notes")
    assert wire.last.url.path == f"{BASE}/ai/backfill/notes" and wire.query() == {"limit": "100"}
    ai.search("notes", text="cats")
    assert wire.json() == {"text": "cats", "metric": "cosine", "limit": 10}
    ai.remove_model("chat")
    assert wire.last.method == "DELETE"
