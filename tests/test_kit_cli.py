"""The ``pawabase`` command, offline: scaffolding, linking, deploying and failing clearly."""

import json

import httpx
import pytest

from pawabase import cli, config
from pawabase.client import Pawabase

ENV = "/platform/v1/envs/development"
KEY = "pb_sk_cli_test_key_0000000000"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GLOBAL_FILE", tmp_path / "home" / "config.json")
    for name in ("PAWABASE_URL", "PAWABASE_API_KEY", "PAWABASE_PROJECT", "PAWABASE_ENVIRONMENT", "PAWABASE_BRANCH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


class Gateway:
    def __init__(self):
        self.requests = []
        self.active = None
        self.refuse = None

    def __call__(self, request):
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, dict(request.url.params), body))
        path = request.url.path.replace("/envs/production", "/envs/development")
        if path == f"{ENV}/function-deployments" and request.method == "GET":
            return httpx.Response(200, json={"data": [self.active] if self.active else []})
        if path == f"{ENV}/function-deployments" and request.method == "POST":
            if self.refuse:
                return httpx.Response(422, json={"detail": {"message": "The new code does not load, so it was not activated.", "problems": self.refuse}})
            self.active = {"id": "d" * 32, "status": "active", "branch": body["branch"], "checksum": __import__("hashlib").sha256(__import__("base64").b64decode(body["archive"])).hexdigest()}
            return httpx.Response(201, json={"deployment": self.active, "functions": [{"name": "hello"}]})
        if path == f"{ENV}/functions":
            return httpx.Response(200, json={"data": [{"name": "hello", "source": "deployment", "policy": "public", "description": "Say hello"}], "errors": []})
        if path.endswith("/invoke"):
            return httpx.Response(200, json={"status": "succeeded", "result": {"ok": True}})
        return httpx.Response(404, json={"detail": "not found"})


@pytest.fixture
def gateway(monkeypatch):
    fake = Gateway()

    def client_of(settings):
        settings.require("url", "api_key")
        client = Pawabase(settings.url, settings.api_key, environment=settings.environment, retries=0)
        client._client = httpx.Client(transport=httpx.MockTransport(fake), base_url=settings.url, headers=client.headers)
        return client

    monkeypatch.setattr(cli, "client_of", client_of)
    return fake


def run(*argv):
    return cli.main(list(argv))


def configured(tmp_path):
    cli.main(["init"])
    (tmp_path / "pawabase.toml").write_text('url = "https://gw.test"\n')
    (tmp_path / ".env").write_text(f"PAWABASE_API_KEY={KEY}\n")


def test_init_scaffolds_a_working_project_and_keeps_the_key_out_of_git(tmp_path, capsys):
    assert run("init") == 0
    assert (tmp_path / "functions/hello.py").exists() and (tmp_path / "tests/test_hello.py").exists() and (tmp_path / ".env.example").exists()
    assert ".env" in (tmp_path / ".gitignore").read_text().splitlines()
    assert run("init") == 0 and "already set up" in capsys.readouterr().out  # idempotent
    # The scaffolded test passes against the scaffolded function.
    import os
    import subprocess
    import sys

    env = {**os.environ, "PYTHONPATH": str(__import__("pathlib").Path(cli.__file__).resolve().parents[1])}
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=tmp_path, capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stdout + done.stderr


def test_link_writes_the_environment_but_never_a_key(tmp_path):
    assert run("--url", "https://gw.test", "--api-key", KEY, "link", "-e", "staging") == 0
    text = (tmp_path / "pawabase.toml").read_text()
    assert 'environment = "staging"' in text and "pb_sk" not in text


def test_missing_settings_exit_2_and_say_what_to_do(tmp_path, capsys):
    run("init")
    assert run("deploy") == 2
    err = capsys.readouterr().err
    assert "PAWABASE_API_KEY" in err and "pawabase login" in err


def test_dry_run_builds_and_checks_but_uploads_nothing(tmp_path, gateway, capsys):
    configured(tmp_path)
    assert run("deploy", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "functions: hello" in out and "Dry run" in out
    assert gateway.requests == []


def test_deploy_uploads_once_then_says_nothing_changed(tmp_path, gateway, capsys):
    configured(tmp_path)
    assert run("deploy") == 0
    first = [r for r in gateway.requests if r[0] == "POST"][0]
    assert first[3]["branch"] == "main" and first[3]["manifest"]["functions"][0]["name"] == "hello" and first[3]["manifest"]["kit"]
    assert "Deployed to development" in capsys.readouterr().out
    assert run("deploy") == 0
    assert "No changes" in capsys.readouterr().out and len([r for r in gateway.requests if r[0] == "POST"]) == 1
    assert run("deploy", "--force") == 0 and len([r for r in gateway.requests if r[0] == "POST"]) == 2


def test_a_branch_deploys_beside_main(tmp_path, gateway):
    configured(tmp_path)
    assert run("deploy", "--branch", "feature-x") == 0
    posted = [r for r in gateway.requests if r[0] == "POST"][0]
    assert posted[3]["branch"] == "feature-x"
    assert [r for r in gateway.requests if r[0] == "GET" and r[2].get("branch")][0][2]["branch"] == "feature-x"


def test_broken_code_is_stopped_before_anything_is_uploaded(tmp_path, gateway, capsys):
    configured(tmp_path)
    (tmp_path / "functions/bad.py").write_text("def broken(:\n")
    assert run("deploy") == 1
    assert "do not import" in capsys.readouterr().err and gateway.requests == []


def test_credentials_in_source_stop_the_deploy(tmp_path, gateway, capsys):
    configured(tmp_path)
    (tmp_path / "functions/leak.py").write_text('KEY = "pb_sk_abcdefghijklmnopqrstuvwxyz"\n')
    assert run("deploy") == 1
    assert "functions/leak.py:1" in capsys.readouterr().err and gateway.requests == []


def test_a_refusal_from_the_deployment_lists_each_problem(tmp_path, gateway, capsys):
    configured(tmp_path)
    gateway.refuse = ["functions/hello.py: ImportError: No module named 'left_out'"]
    assert run("deploy") == 1
    err = capsys.readouterr().err
    assert "was refused" in err and "left_out" in err


def test_production_needs_a_yes(tmp_path, gateway, capsys):
    configured(tmp_path)
    assert run("deploy", "-e", "production") == 2
    assert "--yes" in capsys.readouterr().err and not [r for r in gateway.requests if r[0] == "POST"]
    assert run("deploy", "-e", "production", "--yes") == 0


def test_functions_lists_what_is_deployed_and_what_is_only_local(tmp_path, gateway, capsys):
    configured(tmp_path)
    capsys.readouterr()
    (tmp_path / "functions/extra.py").write_text("from pawabase.functions import function\n@function('extra')\nasync def extra(ctx): return 1\n")
    assert run("functions", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    states = {r["function"]: r["state"] for r in rows}
    assert states == {"hello": "deployed + local", "extra": "local only (not deployed)"}


def test_invoke_local_runs_the_local_copy(tmp_path, gateway, capsys, monkeypatch):
    configured(tmp_path)
    from pawabase.emulator import Emulator

    async def fake_run(self, name, input, auth, *, trigger, request, branch_override=None):
        from pawabase.invoke import invoke
        from pawabase.testing import FakeRuntime

        return await invoke(__import__("pawabase.functions", fromlist=["get_exact"]).get_exact(self.project_key, name), input, runtime=FakeRuntime(), auth=auth, trigger=trigger)

    async def no_close(self):
        return None

    monkeypatch.setattr(Emulator, "_run", fake_run)
    monkeypatch.setattr(Emulator, "aclose", no_close)
    assert run("invoke", "hello", "--local", "--data", '{"name": "Grace"}') == 0
    assert json.loads(capsys.readouterr().out.split("log:")[-1].split("\n", 1)[1])["result"] == {"message": "Hello, Grace!"}
    assert run("invoke", "nope", "--local") == 2


def test_bad_data_is_a_usage_error(tmp_path, gateway, capsys):
    configured(tmp_path)
    assert run("invoke", "hello", "--data", "{not json") == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_a_refusal_in_the_platforms_real_body_shape_lists_each_problem(tmp_path, gateway, capsys):
    configured(tmp_path)
    original = gateway.__call__

    def flat(request):
        if request.method == "POST":
            return httpx.Response(422, json={"message": "The new code does not load, so it was not activated.", "problems": ["functions/x.py: ModuleNotFoundError: No module named 'PIL'"]})
        return original(request)

    gateway.__class__.__call__ = lambda self, request: flat(request)
    try:
        assert run("deploy") == 1
    finally:
        gateway.__class__.__call__ = lambda self, request: original(request)
    assert "No module named 'PIL'" in capsys.readouterr().err
