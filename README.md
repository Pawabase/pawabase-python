# pawabase

The Python kit for [Pawabase](https://github.com/Pawabase/pawabase): write custom **functions** in your own repository, test them without a server, **deploy** them with an API
key, and **emulate** them on your machine against a real deployment. It also contains a small client for calling a project from scripts and servers.

It needs only `httpx` and Python 3.11+. It talks to a deployment through its public API and contains no server code.

```bash
pip install pawabase
pawabase --version
```

## Quick start

```bash
pawabase init                  # functions/hello.py, tests/test_hello.py, .env.example
pawabase link my-project       # writes pawabase.toml (project, environment): never the key
echo 'PAWABASE_URL=https://api.example.com'   >> .env
echo 'PAWABASE_API_KEY=pb_sk_...'             >> .env     # a SECRET key of the environment

pawabase test                  # unit tests, no server needed
pawabase emulate --watch       # run locally against the deployment, on http://127.0.0.1:8787
pawabase deploy                # upload and activate
```

## A function

```python
# functions/hello.py
from pawabase.functions import FunctionError, function

@function("hello", policy="public", input_fields=[{"name": "name", "type": "string"}])
async def hello(ctx):
    name = ctx.input.get("name") or "world"
    if name == "nobody":
        raise FunctionError("Say hello to someone.", status=422, code="no_one")
    ctx.log("greeting", name=name)
    await ctx.runtime.emit("greeted", {"name": name})
    return {"message": f"Hello, {name}!"}
```

The same file runs on a deployment, under `pawabase emulate`, and in a test:

```python
from pawabase.testing import FakeRuntime, call

async def test_hello():
    runtime = FakeRuntime()
    result = await call("hello", {"name": "Ada"}, runtime=runtime, project_dir="functions")
    assert result.ok and result.result == {"message": "Hello, Ada!"}
    assert runtime.emitted == [("greeted", {"name": "Ada"})]
```

`FakeRuntime` runs real SQL on in-memory SQLite and records events, queued flows and functions, mail, outbound HTTP and storage writes.

## Commands

| Command | |
| --- | --- |
| `pawabase deploy [--branch NAME \| --git-branch] [--dry-run] [--force] [-y]` | Check, bundle and upload your functions. Refuses broken code and credentials. Says "No changes" if nothing changed |
| `pawabase emulate [--watch] [--port 8787]` | A local gateway: your functions run here, everything else is forwarded to the deployment |
| `pawabase trigger event\|schedule\|flow NAME [-d JSON]` | Fire one; subscribers run where their code lives |
| `pawabase invoke NAME [-d JSON] [--local] [--as-user ID]` | Call the deployed function, or your local copy |
| `pawabase functions \| deployments \| rollback \| branches \| logs [-f]` | Inspect and operate |
| `pawabase login \| logout \| link \| init \| whoami \| test` | Setup |

Settings come from flags, `PAWABASE_URL`, `PAWABASE_API_KEY`, `PAWABASE_PROJECT`, `PAWABASE_ENVIRONMENT`, `PAWABASE_BRANCH`, a git-ignored `.env`, `pawabase.toml`, and
`pawabase login`, in that order. The key is never written to `pawabase.toml`.

Exit status: `0` success, `1` the operation failed, `2` something is missing or misspelled.

## Calling a project

```python
from pawabase import Pawabase

with Pawabase("https://api.example.com", "pb_sk_...", project="my-project", environment="development") as pb:
    pb.list("orders", sort="-created_at", limit=20)
    pb.create("orders", {"total": 12.5})
    pb.invoke_function("hello", {"name": "Ada"})
    pb.emit_event("order.paid", {"id": 7})
```

`AsyncPawabase` has the same methods, each returning an awaitable. Reads are retried on connection errors and `502/503/504`, `429` waits as the gateway asks, and failures raise
`PawabaseError` with `.status_code`, `.code`, `.message` and `.problems`.

## Documentation

- Writing functions: [`code/functions.mdx`](https://github.com/Pawabase/pawabase-docs/blob/main/code/functions.mdx)
- The command line, deploying, emulating: [`code/cli.mdx`](https://github.com/Pawabase/pawabase-docs/blob/main/code/cli.mdx)

## Develop

```bash
git clone https://github.com/Pawabase/pawabase-python && cd pawabase-python
pip install -e .[test]               # editable install
python -m pytest
uv build                             # wheel and sdist into dist/
```

BSD 3-Clause licensed.
