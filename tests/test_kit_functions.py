"""The function kit: registering, validating, loading and reloading."""

import textwrap

import pytest

from pawabase import codec
from pawabase.functions import (
    FunctionError,
    InputError,
    clear_functions,
    function,
    get_exact,
    list_functions,
    load_functions,
    validate_input,
)


def test_function_names_and_async_are_enforced():
    with pytest.raises(ValueError):
        function("not a name!")(lambda ctx: None)

    with pytest.raises(TypeError):

        @function("sync-one")
        def sync(ctx):  # not async
            return 1


def test_input_validation_fills_defaults_and_names_every_problem():
    fields = [
        {"name": "sku", "type": "string", "required": True, "max_length": 5},
        {"name": "n", "type": "integer", "minimum": 1, "default": 1},
        {"name": "kind", "type": "string", "enum": ["a", "b"]},
        {"name": "price", "type": "number"},
    ]
    assert validate_input(fields, {"sku": "A"}) == {"sku": "A", "n": 1}
    assert validate_input(fields, {"sku": "A", "price": 3}) ["price"] == 3
    with pytest.raises(InputError) as raised:
        validate_input(fields, {"sku": "TOOLONG", "n": 0, "kind": "z", "price": "x"})
    assert set(raised.value.problems) == {"sku", "n", "kind", "price"}
    with pytest.raises(InputError) as missing:
        validate_input(fields, {})
    assert missing.value.problems == {"sku": "This field is required."}
    # Booleans are not integers, and unknown fields pass through.
    with pytest.raises(InputError):
        validate_input([{"name": "n", "type": "integer"}], {"n": True})
    assert validate_input([{"name": "a", "type": "string"}], {"a": "x", "extra": 1})["extra"] == 1


def write(tmp_path, name, text):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def test_loading_a_project_registers_functions_and_reports_broken_files(tmp_path):
    write(tmp_path, "functions/good.py", """
        from pawabase.functions import function

        @function("good", description="Fine")
        async def good(ctx):
            return 1
        """)
    write(tmp_path, "functions/broken.py", "raise RuntimeError('nope')\n")
    write(tmp_path, "functions/_private.py", "raise RuntimeError('never imported')\n")
    result = load_functions(tmp_path / "functions", project="t1")
    assert result.functions == ["good"]
    assert len(result.errors) == 1 and "broken.py" in result.errors[0] and "nope" in result.errors[0]
    assert get_exact("t1", "good").description == "Fine"
    clear_functions("t1")


def test_helpers_next_to_the_functions_import_and_edits_are_picked_up_on_reload(tmp_path):
    write(tmp_path, "helpers_pkg/__init__.py", "VALUE = 1\n")
    write(tmp_path, "functions/use.py", """
        from pawabase.functions import function
        from helpers_pkg import VALUE

        @function("use")
        async def use(ctx):
            return VALUE
        """)
    load_functions(tmp_path / "functions", project="t2", paths=[tmp_path])
    spec = get_exact("t2", "use")
    assert spec is not None
    write(tmp_path, "helpers_pkg/__init__.py", "VALUE = 22  # a different size: Python's bytecode cache keys on mtime (to the second) and size\n")
    reloaded = load_functions(tmp_path / "functions", project="t2", paths=[tmp_path], reload=True)
    assert not reloaded.errors
    import asyncio

    from pawabase.invoke import invoke
    from pawabase.testing import FakeRuntime

    outcome = asyncio.run(invoke(get_exact("t2", "use"), None, runtime=FakeRuntime()))
    assert outcome.result == 22  # the edited helper, not the one imported the first time
    clear_functions("t2")


def test_projects_do_not_see_each_others_functions(tmp_path):
    write(tmp_path / "a", "functions/f.py", "from pawabase.functions import function\n@function('only-a')\nasync def f(ctx): return 'a'\n")
    write(tmp_path / "b", "functions/f.py", "from pawabase.functions import function\n@function('only-b')\nasync def f(ctx): return 'b'\n")
    load_functions(tmp_path / "a" / "functions", project="pa")
    load_functions(tmp_path / "b" / "functions", project="pb")
    assert [s.name for s in list_functions("pa") if s.project == "pa"] == ["only-a"]
    assert [s.name for s in list_functions("pb") if s.project == "pb"] == ["only-b"]
    clear_functions("pa")
    clear_functions("pb")


def test_the_codec_keeps_types_json_does_not_have():
    import datetime as dt
    from decimal import Decimal

    value = {"when": dt.datetime(2026, 10, 2, 9, 0, tzinfo=dt.UTC), "day": dt.date(2026, 10, 2), "money": Decimal("19.99"), "blob": b"\x00\xffabc", "tags": {"a", "b"}, "nested": [{"x": dt.datetime(2026, 1, 1)}]}
    assert codec.decode(codec.encode(value)) == value
    assert codec.decode({"$dt": "2026-10-02T09:00:00+00:00"}).year == 2026
    assert codec.decode({"plain": 1, "$dt": "x"}) == {"plain": 1, "$dt": "x"}  # a tag only counts when it is the whole object


def test_a_function_can_choose_its_status():
    error = FunctionError("Already paid.", status=409, code="already_paid", details={"order": 7})
    assert (error.status, error.code, error.details) == (409, "already_paid", {"order": 7})
