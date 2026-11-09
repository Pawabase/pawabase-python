"""``pb.ai.agents``, ``pb.ai.knowledge`` and the management services for agents, knowledge bases and connections."""

import io

import httpx
import pytest

from pawabase import PawabaseError
from tests.conftest import make, make_async

BASE = "/platform/v1/envs/production"


def sse(*events):
    return httpx.Response(200, content="".join(f"data: {e}\n\n" for e in events).encode(), headers={"content-type": "text/event-stream"})


def test_agents_list_run_get_and_decide(wire, pb):
    wire.answer(httpx.Response(200, json={"data": [{"name": "support", "description": ""}]}))
    assert pb.ai.agents.list()[0]["name"] == "support"
    wire.answer(httpx.Response(201, json={"id": "r1", "status": "done", "output": "done"}))
    assert pb.ai.agents.run("support", "hi", thread="t1")["output"] == "done"
    assert wire.last.url.path == "/ai/v1/agents/support/runs" and wire.json() == {"input": "hi", "background": False, "thread": "t1"}
    pb.ai.agents.decide("r1", decisions={"c1": True})
    assert wire.last.url.path == "/ai/v1/agent-runs/r1/decide" and wire.json()["decisions"] == {"c1": True}
    pb.ai.agents.get("r1")
    assert wire.last.method == "GET" and wire.last.url.path == "/ai/v1/agent-runs/r1"


def test_an_agent_stream_yields_events_until_done_and_raises_on_an_error_event(wire, pb):
    wire.answer(sse('{"type":"text","text":"Hi"}', '{"type":"done","run":{"id":"r1"}}', "[DONE]"))
    assert [e["type"] for e in pb.ai.agents.stream("support", "hi")] == ["text", "done"]
    assert wire.json()["stream"] is True
    wire.answer(sse('{"type":"error","message":"nope"}'))
    with pytest.raises(PawabaseError) as caught:
        list(pb.ai.agents.stream("support", "hi"))
    assert caught.value.status_code == 502


def test_wait_polls_until_the_run_stops_running(wire, pb):
    statuses = iter(["running", "running", "done"])
    wire.answer(lambda request: httpx.Response(200, json={"id": "r1", "status": next(statuses)}))
    assert pb.ai.agents.wait("r1", interval=0)["status"] == "done"
    assert len(wire.requests) == 3


def test_knowledge_search_add_and_status(wire, pb):
    wire.answer(httpx.Response(200, json={"data": [{"text": "x"}]}))
    assert pb.ai.knowledge.search("docs", "reset", limit=3) == [{"text": "x"}]
    assert wire.last.url.path == "/ai/v1/knowledge/docs/search" and wire.json() == {"query": "reset", "limit": 3}
    pb.ai.knowledge.add_document("docs", text="hello", title="T", source="s")
    assert wire.json() == {"title": "T", "source": "s", "text": "hello"}
    pb.ai.knowledge.add_file("docs", io.BytesIO(b"x"), filename="a.txt", title="A")
    assert wire.last.headers["content-type"].startswith("multipart/form-data") and b'filename="a.txt"' in wire.last.content
    pb.ai.knowledge.document("docs", "d1")
    assert wire.last.url.path == "/ai/v1/knowledge/docs/documents/d1"


async def test_the_async_client_streams_and_waits(wire):
    pb = make_async(wire)
    wire.answer(sse('{"type":"text","text":"Hi"}', "[DONE]"))
    assert [e["type"] async for e in pb.ai.agents.stream("support", "hi")] == ["text"]
    statuses = iter(["running", "done"])
    wire.answer(lambda request: httpx.Response(200, json={"id": "r1", "status": next(statuses)}))
    assert (await pb.ai.agents.wait("r1", interval=0))["status"] == "done"
    await pb.close()


def test_management_for_agents_knowledge_connections_and_the_rest(wire):
    sk = make(wire, key="pb_sk_x", environment="production")
    wire.answer(httpx.Response(200, json={"data": []}))
    m = sk.manage
    m.agents.run("support", "hi", background=True)
    assert wire.last.url.path == f"{BASE}/agents/support/runs" and wire.json() == {"input": "hi", "background": True}
    m.agent_runs.list(agent="support", status="waiting")
    assert wire.last.url.path == f"{BASE}/agent-runs" and wire.query() == {"agent": "support", "status": "waiting"}
    m.agent_runs.decide("r1", approve=True)
    assert wire.last.url.path == f"{BASE}/agent-runs/r1/decide" and wire.json()["approve"] is True
    m.agent_runs.tools()
    assert wire.last.url.path == f"{BASE}/agent-tools"
    m.knowledge_bases.add_document("docs", url="https://example.com")
    assert wire.last.url.path == f"{BASE}/knowledge-bases/docs/documents"
    m.knowledge_bases.reindex_document("docs", "d1")
    assert wire.last.url.path == f"{BASE}/knowledge-bases/docs/documents/d1/reindex"
    m.knowledge_bases.search("docs", "q")
    assert wire.json() == {"query": "q", "limit": 5}
    m.knowledge_bases.sync("docs", "notion")
    assert wire.json() == {"connection": "notion"}
    m.connections.test("notion")
    assert wire.last.url.path == f"{BASE}/connections/notion/test"
    m.buckets.create_folder("files", "a/b")
    assert wire.json() == {"prefix": "a/b"}
    m.buckets.delete_folder("files", "a/b")
    assert wire.last.method == "DELETE" and wire.last.url.path == f"{BASE}/buckets/files/folders/a/b"
    m.database.save_query("q", "select 1")
    assert wire.last.url.path == f"{BASE}/database/queries"
    m.mail_templates.rename("a", "b")
    assert wire.json() == {"to": "b"}
    m.mail.save_kit(folders=["x"])
    assert wire.last.method == "PUT" and wire.last.url.path == f"{BASE}/mail/kit"
    m.observability.bandwidth(minutes=60)
    assert wire.last.url.path == f"{BASE}/observability/bandwidth" and wire.query() == {"minutes": "60"}
    m.cache.stats()
    assert wire.last.url.path == "/platform/v1/cache"
