"""Agents, their runs, knowledge bases and connections.

An agent is a model with instructions, tools and knowledge, run on the server with approvals, limits and threads.

    pb.manage.agents.create({"name": "support", "model": "chat", "instructions": "Answer from the docs.", "knowledge": ["docs"]})
    run = pb.manage.agents.run("support", "How do I reset a password?")
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._base import Definitions, Service, seg


class Agents(Definitions):
    kind = "agents"

    def run(self, name: str, input: str, *, thread: str | None = None, background: bool = False) -> Any:
        """Run an agent as the studio would. *thread* carries a conversation on; *background* returns before it finishes."""
        body = {"input": input, "background": background}
        if thread:
            body["thread"] = thread
        return self.post(seg(self.kind, name, "runs"), body)


class AgentRuns(Service):
    """What each run did, step by step, and the ones waiting for someone to approve a tool call."""

    def list(self, *, agent: str | None = None, status: str | None = None, thread: str | None = None, limit: int | None = None, offset: int | None = None) -> Any:
        """Newest first."""
        return self.get("/agent-runs", agent=agent, status=status, thread=thread, limit=limit, offset=offset)

    def get_one(self, run_id: str) -> Any:
        return self.get(seg("agent-runs", run_id))

    def decide(self, run_id: str, *, decisions: Mapping[str, bool] | None = None, approve: bool | None = None, background: bool = False) -> Any:
        """Approve or decline what a run is waiting on: a decision per call id, and *approve* for any not named."""
        body: dict[str, Any] = {"decisions": dict(decisions or {}), "background": background}
        if approve is not None:
            body["approve"] = approve
        return self.post(seg("agent-runs", run_id, "decide"), body)

    def remove(self, run_id: str) -> Any:
        return self.delete(seg("agent-runs", run_id))

    def tools(self) -> Any:
        """The tools an agent can be given, by kind."""
        return self.get("/agent-tools")


class KnowledgeBases(Definitions):
    """Documents split and embedded so agents can search them by meaning and by words."""

    kind = "knowledge-bases"

    def documents(self, name: str, *, limit: int | None = None, offset: int | None = None) -> Any:
        return self.get(seg(self.kind, name, "documents"), limit=limit, offset=offset)

    def add_document(self, name: str, *, text: str | None = None, url: str | None = None, title: str = "", source: str = "") -> Any:
        """Add text, or a web page to read (*url*). Adding the same *source* again replaces it."""
        body = {"title": title, "source": source}
        if text is not None:
            body["text"] = text
        if url is not None:
            body["url"] = url
        return self.post(seg(self.kind, name, "documents"), body)

    def add_file(self, name: str, file: Any, *, filename: str = "document", title: str = "", source: str = "") -> Any:
        """Add a file (PDF, Word, HTML or text): bytes or an open binary file."""
        return self._c._call(
            "POST", self.path(seg(self.kind, name, "documents")), files={"file": (filename, file)}, data={k: v for k, v in {"title": title, "source": source}.items() if v}
        )

    def remove_document(self, name: str, document_id: str) -> Any:
        return self.delete(seg(self.kind, name, "documents", document_id))

    def reindex_document(self, name: str, document_id: str) -> Any:
        """Split and embed a document again, for one that failed or after changing the base's chunk size."""
        return self.post(seg(self.kind, name, "documents", document_id, "reindex"))

    def search(self, name: str, query: str, *, limit: int = 5) -> Any:
        """What an agent would find."""
        return self.post(seg(self.kind, name, "search"), {"query": query, "limit": limit})

    def sync(self, name: str, connection: str) -> Any:
        """Bring in pages from a Notion connection; pages unchanged since last time are skipped."""
        return self.post(seg(self.kind, name, "sync"), {"connection": connection})


class Connections(Definitions):
    """Stored access to outside services (MCP servers, Notion, web search) that agents use as tools. The secret is sealed on arrival."""

    kind = "connections"

    def test(self, name: str) -> Any:
        """Call the service with the stored key and say what was found, or why it could not be reached."""
        return self.post(seg(self.kind, name, "test"))
