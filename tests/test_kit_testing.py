"""``pawabase.testing``: functions under test with real SQL and recorded side effects."""

import textwrap

from pawabase.testing import FakeRuntime, authenticated, call

CODE = textwrap.dedent(
    '''
    from pawabase.functions import FunctionError, function

    @function("stock.reserve", input_fields=[{"name": "sku", "type": "string", "required": True}, {"name": "n", "type": "integer", "default": 1, "minimum": 1}])
    async def reserve(ctx):
        async with ctx.runtime.transaction() as db:
            moved = await db.execute("UPDATE stock SET on_hand = on_hand - ? WHERE sku = ? AND on_hand >= ?", [ctx.input["n"], ctx.input["sku"], ctx.input["n"]])
            if not moved:
                raise FunctionError("Not enough stock.", status=409, code="insufficient_stock")
        await ctx.runtime.emit("stock.reserved", {"sku": ctx.input["sku"], "n": ctx.input["n"], "by": ctx.auth.get("user_id")})
        ctx.log("reserved", sku=ctx.input["sku"])
        return {"left": await (await ctx.runtime.db()).scalar("SELECT on_hand FROM stock WHERE sku = ?", [ctx.input["sku"]])}

    @function("crash")
    async def crash(ctx):
        return 1 / 0

    @function("notify")
    async def notify(ctx):
        reply = await ctx.runtime.http_request("POST", "https://api.example.test/send", json={"to": ctx.input["to"]})
        await ctx.runtime.send_mail([ctx.input["to"]], "Hi", text="hello")
        return reply["body"]
    '''
)


def runtime():
    return FakeRuntime(sql="CREATE TABLE stock (id INTEGER PRIMARY KEY, sku TEXT, on_hand INTEGER); INSERT INTO stock (sku, on_hand) VALUES ('A', 3);")


async def test_real_sql_events_logs_and_the_caller(tmp_path):
    (tmp_path / "functions").mkdir()
    (tmp_path / "functions" / "s.py").write_text(CODE)
    rt = runtime()
    result = await call("stock.reserve", {"sku": "A", "n": 2}, runtime=rt, auth=authenticated("u7"), project_dir=tmp_path / "functions", project="t-sql")
    assert result.ok and result.result == {"left": 1} and result.status == 200
    assert rt.emitted == [("stock.reserved", {"sku": "A", "n": 2, "by": "u7"})]
    assert result.logs == [{"message": "reserved", "sku": "A"}]


async def test_a_function_error_is_an_answer_and_the_transaction_rolled_back(tmp_path):
    (tmp_path / "functions").mkdir()
    (tmp_path / "functions" / "s.py").write_text(CODE)
    rt = runtime()
    refused = await call("stock.reserve", {"sku": "A", "n": 9}, runtime=rt, project_dir=tmp_path / "functions", project="t-err")
    assert not refused.ok and refused.status == 409 and refused.body == {"error": "insufficient_stock", "message": "Not enough stock."}
    assert rt.emitted == [] and (await (await rt.db()).scalar("SELECT on_hand FROM stock")) == 3


async def test_validation_and_crashes_are_answers_too(tmp_path):
    (tmp_path / "functions").mkdir()
    (tmp_path / "functions" / "s.py").write_text(CODE)
    invalid = await call("stock.reserve", {"n": 0}, runtime=runtime(), project_dir=tmp_path / "functions", project="t-val")
    assert invalid.status == 422 and set(invalid.error["details"]) == {"sku", "n"}
    crashed = await call("crash", runtime=runtime(), project="t-val")
    assert crashed.status == 500 and "ZeroDivisionError" in crashed.error["message"] and "ZeroDivisionError" in crashed.traceback


async def test_outbound_calls_and_mail_are_recorded_not_made(tmp_path):
    (tmp_path / "functions").mkdir()
    (tmp_path / "functions" / "s.py").write_text(CODE)
    rt = FakeRuntime(http={("POST", "https://api.example.test/send"): {"status": 202, "body": {"queued": True}}})
    result = await call("notify", {"to": "a@b.test"}, runtime=rt, project_dir=tmp_path / "functions", project="t-out")
    assert result.result == {"queued": True}
    assert rt.http_calls[0]["json"] == {"to": "a@b.test"} and rt.mails[0]["to"] == ["a@b.test"]



async def test_an_http_style_exception_is_the_answer_it_describes():
    from pawabase.functions import clear_functions, function

    class HttpLike(Exception):
        status_code, detail = 422, {"error": "checkout_failed", "message": "Only 0 available."}

    class Plain(Exception):
        status_code, detail = 404, "Not here"

    @function("throws-dict")
    async def throws_dict(ctx):
        raise HttpLike()

    @function("throws-text")
    async def throws_text(ctx):
        raise Plain()

    try:
        loud = await call("throws-dict", project="*")
        assert (loud.status, loud.body) == (422, {"error": "checkout_failed", "message": "Only 0 available."}) and loud.traceback is None
        quiet = await call("throws-text", project="*")
        assert (quiet.status, quiet.body) == (404, {"error": "http_error", "message": "Not here"})
    finally:
        clear_functions("*")
