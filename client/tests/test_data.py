import httpx
import pytest

from pawabase import ConflictError, NotFoundError, QueryError
from pawabase.data import encode_filter
from tests.conftest import make


def page(rows, total=None, page=1, per_page=20):
    return httpx.Response(200, json={"data": rows, "page": page, "per_page": per_page, "total": total if total is not None else len(rows)})


def test_filters_sorting_selection_and_paging_are_sent_as_the_server_reads_them(wire, pb):
    wire.answer(page([{"id": "1"}]))
    result = (
        pb.from_("posts").eq("status", "live").gte("views", 10).like("title", "a*").order("created_at", "desc").order("title")
        .select("id", "title").expand("author").page(2).per_page(5).execute()
    )
    assert wire.last.url.path == "/rest/v1/posts"
    assert wire.query() == {
        "filter[status]": "eq.live", "filter[views]": "gte.10", "filter[title]": "like.a*", "sort": "-created_at,title",
        "select": "id,title", "expand": "author", "page": "2", "per_page": "5",
    }
    assert result.data == [{"id": "1"}] and result.total == 1


def test_booleans_and_null_use_is_because_eq_false_matches_true_rows(wire, pb):
    wire.answer(page([]))
    pb.from_("posts").eq("draft", False).eq("deleted_at", None).neq("flag", True).execute()
    assert wire.query() == {"filter[draft]": "is.false", "filter[deleted_at]": "is.null", "filter[flag]": "isnot.true"}


def test_a_second_filter_on_one_field_raises_before_anything_is_sent(wire, pb):
    with pytest.raises(QueryError, match="one filter per field"):
        pb.from_("posts").gte("views", 1).lte("views", 9)
    assert wire.requests == []


def test_in_lists_and_their_edge_cases(wire, pb):
    wire.answer(page([]))
    pb.from_("posts").in_("id", ["a", "b"]).not_in("kind", [1, 2]).execute()
    assert wire.query() == {"filter[id]": "in.a,b", "filter[kind]": "nin.1,2"}
    wire.requests.clear()
    assert pb.from_("posts").in_("id", []).execute().data == [] and wire.requests == []  # nothing can match: answered locally
    with pytest.raises(QueryError, match="comma"):
        pb.from_("posts").in_("id", ["a,b"])
    assert encode_filter("x", "nin", []) == "skip"


def test_where_takes_keywords_and_operators(wire, pb):
    wire.answer(page([]))
    pb.from_("posts").where(status="live", views={"gte": 3}).execute()
    assert wire.query() == {"filter[status]": "eq.live", "filter[views]": "gte.3"}
    with pytest.raises(QueryError):
        pb.from_("posts").where(views={"gte": 1, "lte": 5})


def test_single_record_writes_and_reads(wire, pb):
    wire.answer(httpx.Response(201, json={"id": "7", "title": "Hi"}))
    assert pb.from_("posts").create(title="Hi")["id"] == "7"
    assert wire.last.method == "POST" and wire.json() == {"title": "Hi"}
    wire.answer(httpx.Response(200, json={"id": "7"}))
    pb.from_("posts").get("7", expand=["author"])
    assert wire.last.url.path == "/rest/v1/posts/7" and wire.query() == {"expand": "author"}
    pb.from_("posts").update("7", title="New")
    assert wire.last.method == "PATCH" and wire.json() == {"title": "New"}
    wire.answer(httpx.Response(204))
    assert pb.from_("posts").delete("7") is None and wire.last.method == "DELETE"


def test_missing_records_and_conflicts_are_typed_errors(wire, pb):
    wire.answer(httpx.Response(404, json={"error": "not_found", "message": "nope"}))
    with pytest.raises(NotFoundError):
        pb.from_("posts").get("x")
    assert pb.from_("posts").maybe_get("x") is None
    wire.answer(httpx.Response(409, json={"error": "unique_violation", "message": "taken", "details": {"columns": ["email"]}}))
    with pytest.raises(ConflictError) as raised:
        pb.from_("users").create(email="a@x.io")
    assert raised.value.code == "unique_violation" and raised.value.details == {"columns": ["email"]}


def test_iterating_walks_every_page(wire, pb):
    pages = {1: page([{"id": 1}, {"id": 2}], total=3, page=1, per_page=2), 2: page([{"id": 3}], total=3, page=2, per_page=2)}
    wire.answer(lambda request: pages[int(request.url.params.get("page", 1))])
    assert [row["id"] for row in pb.from_("posts").iterate(page_size=2)] == [1, 2, 3]
    assert [row["id"] for row in pb.from_("posts").all(page_size=2, max_records=2)] == [1, 2]


def test_count_needs_a_total_and_single_needs_a_row(wire, pb):
    wire.answer(httpx.Response(200, json={"data": [], "page": 1, "per_page": 1, "total": None}))
    with pytest.raises(QueryError, match="cannot count"):
        pb.from_("posts").count()
    with pytest.raises(NotFoundError):
        pb.from_("posts").single()


def test_create_many_collects_failures(wire, pb):
    answers = [httpx.Response(201, json={"id": "1"}), httpx.Response(422, json={"error": "invalid"}), httpx.Response(201, json={"id": "3"})]
    wire.answer(lambda request: answers.pop(0))
    result = pb.from_("posts").create_many([{"a": 1}, {"a": 2}, {"a": 3}])
    assert [r["id"] for r in result.created] == ["1", "3"] and [i for i, _ in result.failed] == [1]


async def test_the_async_client_has_the_same_methods(wire, apb):
    wire.answer(page([{"id": "1"}, {"id": "2"}], total=2))
    assert [r["id"] async for r in apb.from_("posts").eq("status", "live").iterate()] == ["1", "2"]
    assert (await apb.from_("posts").eq("status", "live").first())["id"] == "1"
    wire.answer(httpx.Response(201, json={"id": "9"}))
    assert (await apb.from_("posts").create(t=1))["id"] == "9"
    wire.answer(httpx.Response(200, json={"data": [], "page": 1, "per_page": 1, "total": 12}))
    assert await apb.from_("posts").count() == 12


def test_a_connection_failure_is_a_network_error(wire):
    from pawabase import NetworkError

    def refuse(request):
        raise httpx.ConnectError("refused")

    wire.answer(refuse)
    with pytest.raises(NetworkError):
        make(wire).from_("posts").execute()


def test_paginate_false_asks_for_every_match_and_drops_page_settings(wire, pb):
    wire.answer(httpx.Response(200, json={"data": [{"id": "1"}, {"id": "2"}], "page": None, "per_page": None, "total": 2}))
    result = pb.from_("posts").per_page(5).page(3).paginate(False).execute()
    assert wire.query() == {"paginate": "false"}
    assert result.page is None and result.per_page is None and result.has_next is False and len(result) == 2
