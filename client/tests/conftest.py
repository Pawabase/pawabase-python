import httpx
import pytest

from pawabase import AsyncPawabase, Pawabase

URL = "http://gateway.test"


class Wire:
    """A fake gateway: records every request and answers with what the test last said (a response, copied per request, or a function)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handler = None

    def answer(self, handler) -> None:
        """From now on, answer with *handler*: a ``httpx.Response`` (sent afresh each time) or a function of the request."""
        self.handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.handler
        if handler is None:
            return httpx.Response(200, json={})
        if isinstance(handler, httpx.Response):
            return httpx.Response(handler.status_code, headers=handler.headers, content=handler.content)
        return handler(request)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def query(self, index: int = -1) -> dict[str, str]:
        return dict(self.requests[index].url.params)

    def json(self, index: int = -1):
        import json

        return json.loads(self.requests[index].content or b"null")


@pytest.fixture
def wire():
    return Wire()


def make(wire: Wire, key: str = "pb_pk_test", **options) -> Pawabase:
    client = Pawabase(URL, key, retries=0, **options)
    client._client = httpx.Client(transport=httpx.MockTransport(wire), base_url=URL, headers=client.headers)
    return client


def make_async(wire: Wire, key: str = "pb_pk_test", **options) -> AsyncPawabase:
    client = AsyncPawabase(URL, key, retries=0, **options)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(wire), base_url=URL, headers=client.headers)
    return client


@pytest.fixture
def pb(wire):
    return make(wire)


@pytest.fixture
def apb(wire):
    return make_async(wire)
