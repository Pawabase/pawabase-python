"""The ``pawabase`` command.

::

    pawabase login --url https://api.example.com        remember a URL and API key on this machine
    pawabase link my-project --environment development   say which project this folder deploys to (writes pawabase.toml)
    pawabase init                                         scaffold functions/, a test and .env.example
    pawabase deploy [--branch NAME | --git-branch]        upload and activate your functions on the deployment
    pawabase emulate [--watch]                            run them locally against the deployment
    pawabase invoke NAME --data '{"x": 1}' [--local]      call a function (deployed, or your local copy)
    pawabase trigger event|schedule|flow NAME             fire an event, a schedule or a flow
    pawabase functions | deployments | logs [-f] | rollback | branches | whoami

Settings come from flags, ``PAWABASE_*`` variables, ``.env``, ``pawabase.toml`` and ``pawabase login`` (see :mod:`pawabase.config`). Exit status: ``0`` success,
``1`` the operation failed, ``2`` something is missing or misspelled.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__, bundle, config
from .client import Pawabase, PawabaseError
from .emulator import Emulator
from .functions import list_functions, load_functions

PRODUCTION_NAMES = ("production", "prod", "live")


# ── output ───────────────────────────────────────────────────────────────


class Out:
    """Plain output with a little colour on a terminal, or JSON when asked."""

    def __init__(self, *, as_json: bool = False, quiet: bool = False) -> None:
        self.as_json, self.quiet = as_json, quiet
        self.color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def _paint(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def line(self, text: str = "") -> None:
        if not self.quiet and not self.as_json:
            print(text)

    def ok(self, text: str) -> None:
        self.line(self._paint("32", "✓ ") + text)

    def info(self, text: str) -> None:
        self.line(self._paint("2", text))

    def warn(self, text: str) -> None:
        print(self._paint("33", "! ") + text, file=sys.stderr)

    def fail(self, text: str) -> None:
        print(self._paint("31", "✗ ") + text, file=sys.stderr)

    def data(self, value: Any) -> None:
        """The result of a command: JSON for ``--json``, indented JSON otherwise."""
        print(json.dumps(value, indent=None if self.as_json else 2, default=str))

    def table(self, header: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
        if self.as_json:
            self.data([dict(zip(header, row, strict=False)) for row in rows])
            return
        if not rows:
            self.line(self._paint("2", "(none)"))
            return
        cells = [[str(c if c is not None else "") for c in row] for row in rows]
        widths = [max(len(h), *(len(r[i]) for r in cells)) for i, h in enumerate(header)]
        self.line(self._paint("1", "  ".join(h.ljust(w) for h, w in zip(header, widths, strict=True))))
        for row in cells:
            self.line("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)))


class CliError(Exception):
    """A failure to report without a traceback. ``code`` is the exit status."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


# ── shared plumbing ──────────────────────────────────────────────────────


def settings_of(args: argparse.Namespace, *, git_branch: bool = False) -> config.Settings:
    try:
        return config.load(url=args.url, api_key=args.api_key, project=args.project, environment=args.environment, branch=args.branch, use_git_branch=git_branch or getattr(args, "git_branch", False))
    except config.ConfigError as error:
        raise CliError(str(error), 2) from error


def ask_for_project(settings: config.Settings) -> config.Settings:
    """No project named and a person at the keyboard: ask, and offer to remember the answer in pawabase.toml."""
    if settings.project or not sys.stdin.isatty():
        return settings
    project = input("Project reference (the `ref` of your project in Studio): ").strip()
    if not project:
        return settings
    if input(f"Save it to {settings.root / 'pawabase.toml'}? [Y/n] ").strip().lower() in ("", "y", "yes"):
        config.write_project_file(settings.root, {"project": project})
    return config.with_overrides(settings, project=project)


def client_of(settings: config.Settings) -> Pawabase:
    settings = ask_for_project(settings)
    try:
        settings.require("url", "api_key", "project")
    except config.ConfigError as error:
        raise CliError(str(error), 2) from error
    if (settings.api_key or "").startswith("pb_pk_"):
        raise CliError("PAWABASE_API_KEY is a publishable key (pb_pk_…). Deploying and emulating need the SECRET key (pb_sk_…) of this environment: Studio → this project → "
                       "API keys. A publishable key is the one your app ships to browsers; it cannot manage the project.", 2)
    return Pawabase(settings.url or "", settings.api_key or "", project=settings.project or "", environment=settings.environment)


def read_data(spec: str | None) -> Any:
    """``--data``: inline JSON, ``@file.json``, or ``-`` for stdin."""
    if spec is None:
        return None
    try:
        text = sys.stdin.read() if spec == "-" else Path(spec[1:]).read_text() if spec.startswith("@") else spec
        return json.loads(text) if text.strip() else None
    except (OSError, ValueError) as error:
        raise CliError(f"--data is not valid JSON ({error}).", 2) from error


def describe_error(error: PawabaseError) -> str:
    lines = [error.message]
    lines += [f"  - {problem}" for problem in error.problems]
    if error.status_code == 401:
        lines.append("  The API key was refused. Check PAWABASE_API_KEY (a secret key, pb_sk_…) and that it belongs to this project and environment.")
    elif error.status_code == 403:
        lines.append("  The key lacks a scope for this (deploying needs functions:manage; emulating needs runtime:use) or belongs to another environment.")
    elif error.status_code == 0:
        lines.append("  Check the URL (PAWABASE_URL) and your connection.")
    return "\n".join(lines)


def local_functions(settings: config.Settings, *, reload: bool = False) -> tuple[list[Any], list[str]]:
    """Import the project's functions. Returns ``(specs, import errors)``."""
    paths: list[str | Path] = [settings.root / p for p in settings.paths]
    code = load_functions(settings.functions_dir, project="cli", paths=paths, reload=reload)
    return [spec for spec in list_functions("cli") if spec.project == "cli"], code.errors


# ── commands ─────────────────────────────────────────────────────────────


def cmd_login(args: argparse.Namespace, out: Out) -> int:
    key = args.login_key or os.environ.get("PAWABASE_API_KEY")
    if not key:
        if not sys.stdin.isatty():
            raise CliError("Pass --key, set PAWABASE_API_KEY, or run this in a terminal to be asked.", 2)
        key = getpass.getpass("Secret API key (pb_sk_…): ").strip()
    if not key.startswith("pb_sk_"):
        out.warn("That does not look like a secret key (pb_sk_…). Deploying and emulating need one; publishable keys cannot manage a project.")
    url = (args.url or "").rstrip("/") or None
    if not url and not config.load().url:
        raise CliError("Pass --url (the gateway's address).", 2)
    path = config.write_global({"url": url, "api_key": key})
    out.ok(f"Saved to {path} (readable only by you).")
    return 0


def cmd_logout(_args: argparse.Namespace, out: Out) -> int:
    if config.GLOBAL_FILE.exists():
        config.GLOBAL_FILE.unlink()
    out.ok("Forgot the saved URL and key.")
    return 0


def cmd_link(args: argparse.Namespace, out: Out) -> int:
    settings = settings_of(args)
    data = {"project": args.link_project, "environment": args.environment or settings.environment, "url": args.url or settings.url}
    path = config.write_project_file(settings.root, data)
    out.ok(f"Linked this folder to {args.link_project} ({data['environment']}) in {path.name}.")
    if not settings.api_key:
        out.info("No API key yet: run `pawabase login`, or put PAWABASE_API_KEY=pb_sk_… in .env.")
    return 0


INIT_FUNCTION = '''from pawabase.functions import function


@function("hello", description="Say hello", policy="public", input_fields=[{"name": "name", "type": "string"}])
async def hello(ctx):
    """The smallest function: input in, a result out."""
    name = ctx.input.get("name") or "world"
    ctx.log("greeting", name=name)
    return {"message": f"Hello, {name}!"}
'''
INIT_TEST = '''import asyncio

from pawabase.testing import call


def test_hello():
    # asyncio.run keeps this test free of any pytest plugin; with pytest-asyncio you can write `async def` tests and `await call(...)`.
    result = asyncio.run(call("hello", {"name": "Ada"}, project_dir="functions"))
    assert result.ok and result.result == {"message": "Hello, Ada!"}
'''


def cmd_init(args: argparse.Namespace, out: Out) -> int:
    root = Path(args.directory).resolve()
    (root / "functions").mkdir(parents=True, exist_ok=True)
    created = []
    for relative, content in (("functions/hello.py", INIT_FUNCTION), ("tests/test_hello.py", INIT_TEST), (".env.example", "PAWABASE_URL=\nPAWABASE_API_KEY=pb_sk_...\nPAWABASE_PROJECT=\nPAWABASE_ENVIRONMENT=development\n")):
        target = root / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        created.append(relative)
    gitignore = root / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    if ".env" not in existing.splitlines():
        gitignore.write_text(existing + ("\n" if existing and not existing.endswith("\n") else "") + ".env\n__pycache__/\n")
        created.append(".gitignore")
    out.ok("Created " + ", ".join(created) if created else "Nothing to create: it is already set up.")
    out.info("Next: pawabase link <project>, put your key in .env, then `pawabase deploy` or `pawabase emulate`.")
    return 0


def cmd_whoami(args: argparse.Namespace, out: Out) -> int:
    settings = settings_of(args)
    info = settings.describe()
    if args.json:
        out.data(info)
    else:
        for name in ("url", "project", "environment", "branch", "api_key", "root", "functions"):
            out.line(f"{name:12} {info[name]}  " + out._paint("2", f"({info['sources'].get(name, 'default')})" if name in info["sources"] else ""))
    with client_of(settings) as client:
        try:
            deployed = client.functions(branch=settings.branch if settings.branch != "main" else None)
        except PawabaseError as error:
            raise CliError("The deployment did not accept these settings:\n" + describe_error(error)) from error
    out.ok(f"Connected. {len(deployed.get('data', []))} functions are visible on {settings.environment}" + (f" (branch {settings.branch})" if settings.branch != "main" else "") + ".")
    return 0


def cmd_deploy(args: argparse.Namespace, out: Out) -> int:
    settings = settings_of(args)
    if args.git_branch and settings.branch == "main" and not args.branch:
        out.warn("Not in a git branch (or on main): deploying to main.")
    if args.dir:
        settings = config.with_overrides(settings, functions=args.dir)
    try:
        packed = bundle.build(settings.root, functions=settings.functions, include=settings.include, exclude=settings.exclude, allow_secrets=args.allow_secrets)
    except bundle.BundleError as error:
        raise CliError(str(error)) from error
    specs: list[Any] = []
    if not args.no_check:
        specs, errors = local_functions(settings)
        if errors:
            raise CliError("Your functions do not import, so nothing was uploaded:\n  - " + "\n  - ".join(errors) + "\n  (use --no-check to upload anyway)")
        if not specs:
            raise CliError("No @function was found in the project. Is `functions` the right folder?")
    where = f"{settings.project} / {settings.environment}" + (f" @ {settings.branch}" if settings.branch != "main" else "")
    out.line(f"{len(packed.files)} files, {packed.size / 1024:.1f} KB, checksum {packed.checksum[:12]}")
    for name in packed.skipped[:8]:
        out.info(f"  skipped {name}")
    if len(packed.skipped) > 8:
        out.info(f"  … and {len(packed.skipped) - 8} more skipped")
    if specs:
        out.line("functions: " + ", ".join(sorted(spec.name for spec in specs)))
    manifest = bundle.manifest(settings.branch, config.git_commit(settings.root), [{"name": s.name, "description": s.description, "policy": s.policy, "timeout": s.timeout} for s in specs], packed, __version__)
    if args.dry_run:
        out.ok(f"Dry run: would deploy to {where}. Nothing was uploaded.")
        if args.json:
            out.data({"dry_run": True, "files": len(packed.files), "bytes": packed.size, "checksum": packed.checksum, "functions": [s.name for s in specs]})
        return 0
    if settings.environment in PRODUCTION_NAMES and not args.yes:
        if not sys.stdin.isatty():
            raise CliError(f"{settings.environment} is a production environment: pass --yes to deploy to it without asking.", 2)
        if input(f"Deploy to {where}? [y/N] ").strip().lower() not in ("y", "yes"):
            raise CliError("Cancelled.")
    with client_of(settings) as client:
        try:
            if not args.force:
                active = next((d for d in client.deployments(branch=settings.branch).get("data", []) if d.get("status") == "active"), None)
                if active and active.get("checksum") == packed.checksum:
                    out.ok(f"No changes: deployment {active['id'][:12]} is already active on {where}. (--force deploys anyway)")
                    return 0
            answer = client.deploy_functions(packed.encoded, branch=settings.branch, manifest=manifest, limits={"timeout": settings.timeout, "memory_mb": settings.memory_mb})
        except PawabaseError as error:
            raise CliError(f"The deployment was refused.\n{describe_error(error)}") from error
    deployment = answer["deployment"]
    names = sorted(f["name"] for f in answer.get("functions", []))
    out.ok(f"Deployed to {where} as {deployment['id'][:12]}: {len(names)} functions live.")
    if args.json:
        out.data(answer)
    return 0


def cmd_emulate(args: argparse.Namespace, out: Out) -> int:
    settings = settings_of(args, git_branch=args.git_branch)
    if args.dir:
        settings = config.with_overrides(settings, functions=args.dir)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        out.warn(f"Listening on {args.host}: anyone who can reach this port can run your local functions with your API key's authority over the deployment. Use 127.0.0.1 unless you mean it.")
    emulator = Emulator(settings, host=args.host, port=args.port, watch=args.watch, log=None if args.quiet else out.line)

    def ready(info: dict[str, Any]) -> None:
        out.ok(f"Emulating {settings.project} / {settings.environment}" + (f" @ {settings.branch}" if settings.branch != "main" else "") + f" on http://{args.host}:{args.port}")
        out.line(f"  local functions ({len(info['local'])}): {', '.join(info['local']) or '-'}")
        out.line(f"  served locally: {info['routes_served_locally']} routes, plus /functions/v1 for the above; everything else is forwarded to {settings.url}")
        out.line(f"  deployment knows {len(info['deployed'])} functions, {info['subscriptions']} subscriptions, {info['schedules']} schedules")
        for problem in info["errors"]:
            out.warn(f"does not import: {problem}")
        out.info("  Ctrl-C to stop" + ("; watching for changes" if args.watch else ""))

    try:
        emulator.serve(on_ready=ready)
    except PawabaseError as error:
        raise CliError("Could not start: the deployment did not accept these settings.\n" + describe_error(error)) from error
    except KeyboardInterrupt:
        out.line("")
        out.ok("Stopped.")
    except OSError as error:
        raise CliError(f"Cannot listen on {args.host}:{args.port}: {error}") from error
    return 0


def cmd_invoke(args: argparse.Namespace, out: Out) -> int:
    settings = settings_of(args)
    data = read_data(args.data)
    as_user = {"authenticated": True, "user_id": args.as_user, "kind": "user", "roles": args.role or [], "permissions": args.permission or []} if args.as_user else None
    branch = settings.branch if settings.branch != "main" else None
    if not args.local:
        with client_of(settings) as client:
            try:
                answer = client.run_function(args.function, data, branch=branch, as_user=as_user)
            except PawabaseError as error:
                raise CliError(describe_error(error)) from error
        out.data(answer)
        return 0 if answer.get("status") == "succeeded" else 1
    emulator = Emulator(settings, log=None)
    code = emulator.load_code()
    if code.errors:
        raise CliError("Your functions do not import:\n  - " + "\n  - ".join(code.errors))
    if args.function not in emulator.local_functions:
        raise CliError(f"No local function {args.function!r}. Local: {', '.join(sorted(emulator.local_functions)) or '-'}", 2)

    async def run() -> Any:
        try:
            return await emulator._run(args.function, data, as_user or {"authenticated": False}, trigger="emulator", request=None)
        finally:
            await emulator.aclose()

    try:
        outcome = asyncio.run(run())
    except PawabaseError as error:
        raise CliError(describe_error(error)) from error
    for entry in outcome.logs:
        out.info(f"log: {entry}")
    if outcome.traceback:
        print(outcome.traceback, file=sys.stderr)
    out.data({"status": "succeeded", "result": outcome.result} if outcome.ok else {"status": "failed", **outcome.body})
    return 0 if outcome.ok else 1


def cmd_trigger(args: argparse.Namespace, out: Out) -> int:
    settings = settings_of(args)
    data = read_data(args.data)
    emulator = Emulator(settings, log=None)

    async def run() -> Any:
        try:
            if args.kind == "flow" and not args.local_code:
                return await emulator.trigger_flow(args.name, data)
            if args.kind == "event" and args.remote:
                return await emulator.trigger_event(args.name, data, remote_only=True)
            emulator.load_code()
            await emulator.refresh()
            if args.kind == "event":
                return await emulator.trigger_event(args.name, data)
            if args.kind == "schedule":
                return await emulator.trigger_schedule(args.name)
            return await emulator.trigger_flow(args.name, data)
        finally:
            await emulator.aclose()

    try:
        result = asyncio.run(run())
    except PawabaseError as error:
        raise CliError(describe_error(error)) from error
    except LookupError as error:
        raise CliError(f"Not found: {error}", 2) from error
    for failed in [r for r in result.get("results", []) if r.get("error")]:
        out.warn(f"{failed.get('subscription')}: {failed['error']}")
    out.data(result)
    return 0


def cmd_functions(args: argparse.Namespace, out: Out) -> int:
    settings = settings_of(args)
    branch = settings.branch if settings.branch != "main" else None
    with client_of(settings) as client:
        try:
            deployed = client.functions(branch=branch)
        except PawabaseError as error:
            raise CliError(describe_error(error)) from error
    local, errors = ([], [])
    if (settings.functions_dir).is_dir():
        local, errors = local_functions(settings)
    remote = {f["name"]: f for f in deployed.get("data", [])}
    mine = {s.name: s for s in local}
    rows = []
    for name in sorted({*remote, *mine}):
        state = "deployed + local" if name in remote and name in mine else "deployed only" if name in remote else "local only (not deployed)"
        rows.append([name, state, (remote.get(name) or {}).get("source", ""), (remote.get(name) or mine[name].describe()).get("policy", ""), ((remote.get(name) or {}).get("description") or (mine[name].description if name in mine else ""))[:60]])
    out.table(["function", "state", "from", "policy", "description"], rows)
    for problem in deployed.get("errors", []) + errors:
        out.warn(f"does not import: {problem}")
    return 0


def cmd_deployments(args: argparse.Namespace, out: Out) -> int:
    settings = settings_of(args)
    with client_of(settings) as client:
        try:
            rows = client.deployments(branch=args.branch or None).get("data", [])
        except PawabaseError as error:
            raise CliError(describe_error(error)) from error
    out.table(["id", "branch", "status", "functions", "checksum", "activated"], [[r["id"][:12], r.get("branch"), r["status"], len((r.get("manifest") or {}).get("functions", [])), (r.get("checksum") or "")[:10], str(r.get("activated_at") or "")[:19]] for r in rows])
    return 0


def cmd_rollback(args: argparse.Namespace, out: Out) -> int:
    settings = settings_of(args)
    with client_of(settings) as client:
        try:
            rows = client.deployments(branch=settings.branch).get("data", [])
            target = None
            if args.deployment:
                target = next((r for r in rows if r["id"].startswith(args.deployment)), None)
            else:
                active = next((r for r in rows if r["status"] == "active"), None)
                previous = [r for r in rows if r["status"] == "superseded" and (not active or r["id"] != active["id"])]
                target = previous[0] if previous else None
            if target is None:
                raise CliError("There is no earlier deployment to go back to." if not args.deployment else f"No deployment starting with {args.deployment!r} on branch {settings.branch}.")
            answer = client.activate_deployment(target["id"])
        except PawabaseError as error:
            raise CliError(describe_error(error)) from error
    out.ok(f"Rolled back to {target['id'][:12]}: {len(answer.get('functions', []))} functions live.")
    return 0


def cmd_branches(args: argparse.Namespace, out: Out) -> int:
    settings = settings_of(args)
    with client_of(settings) as client:
        try:
            if args.remove:
                client.remove_function_branch(args.remove)
                out.ok(f"Removed the functions deployed on {args.remove}.")
                return 0
            rows = client.function_branches().get("data", [])
        except PawabaseError as error:
            raise CliError(describe_error(error)) from error
    out.table(["branch", "deployment", "functions"], [[r["branch"], (r.get("deployment_id") or "")[:12], ", ".join(r.get("functions", []))] for r in rows])
    return 0


def format_run(run: dict[str, Any]) -> str:
    status = run.get("status", "?")
    mark = "✓" if status == "succeeded" else "✗"
    when = str(run.get("created_at", ""))[11:19]
    detail = f"  {run['error']}" if run.get("error") else ""
    return f"{when} {mark} {run.get('function', ''):28} {run.get('trigger', ''):8} {run.get('duration_ms', 0) or 0:8.1f}ms  {run.get('branch', 'main')}{detail}"


def cmd_logs(args: argparse.Namespace, out: Out) -> int:
    settings = settings_of(args)
    with client_of(settings) as client:
        try:
            if args.run:
                out.data(client.function_run(args.run))
                return 0
            query = {"function": args.function, "status": args.status, "branch": args.branch or None}
            rows = client.function_runs(limit=args.limit, **query).get("data", [])
            for run in reversed(rows):
                out.line(format_run(run))
            if not args.follow:
                return 0
            after = max((str(r.get("created_at")) for r in rows), default=None)
            out.info("following… Ctrl-C to stop")
            while True:
                time.sleep(1.5)
                fresh = client.function_runs(limit=100, after=after, **query).get("data", [])
                for run in reversed(fresh):
                    out.line(format_run(run))
                if fresh:
                    after = max(str(r.get("created_at")) for r in fresh)
        except PawabaseError as error:
            raise CliError(describe_error(error)) from error
        except KeyboardInterrupt:
            return 0


def cmd_test(args: argparse.Namespace, _out: Out) -> int:
    settings = settings_of(args)
    return subprocess.call([sys.executable, "-m", "pytest", *args.pytest_args], cwd=settings.root)


# ── parser ───────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pawabase", description="Deploy, emulate and operate Pawabase functions.")
    parser.add_argument("--version", action="version", version=f"pawabase {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    # Accepted before or after the command (``pawabase --project p deploy`` and ``pawabase deploy --project p``). SUPPRESS keeps a value given in one place
    # from being overwritten by the other place's default.
    for target in (parser, common):
        group = target.add_argument_group("global options") if target is parser else target
        group.add_argument("--url", default=argparse.SUPPRESS, help="gateway URL (PAWABASE_URL)")
        group.add_argument("--api-key", default=argparse.SUPPRESS, help="secret API key (PAWABASE_API_KEY)")
        group.add_argument("--project", default=argparse.SUPPRESS, help="project reference (PAWABASE_PROJECT)")
        group.add_argument("-e", "--environment", default=argparse.SUPPRESS, help="environment (PAWABASE_ENVIRONMENT; default development)")
        group.add_argument("-b", "--branch", default=argparse.SUPPRESS, help="branch (PAWABASE_BRANCH; default main)")
        group.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
        group.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS, help="only errors and results")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    def add(name: str, handler: Any, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, parents=[common], help=help_, description=help_)
        p.set_defaults(handler=handler)
        return p

    p = add("login", cmd_login, "Remember the gateway URL and a secret API key on this machine")
    p.add_argument("--key", dest="login_key", help="the key (otherwise asked, or PAWABASE_API_KEY)")
    add("logout", cmd_logout, "Forget the saved URL and key")
    p = add("link", cmd_link, "Say which project and environment this folder deploys to (writes pawabase.toml)")
    p.add_argument("link_project", metavar="project")
    p = add("init", cmd_init, "Scaffold functions/, a test and .env.example")
    p.add_argument("directory", nargs="?", default=".")
    add("whoami", cmd_whoami, "Show the resolved settings and check the deployment accepts them")
    p = add("deploy", cmd_deploy, "Upload and activate the project's functions")
    p.add_argument("--dir", help="the functions folder (default: functions, or `functions` in pawabase.toml)")
    p.add_argument("--git-branch", action="store_true", help="deploy to a branch named after the checked-out git branch")
    p.add_argument("--dry-run", action="store_true", help="build and check the bundle, upload nothing")
    p.add_argument("--no-check", action="store_true", help="do not import the functions locally first")
    p.add_argument("--force", action="store_true", help="deploy even if the active deployment is identical")
    p.add_argument("--allow-secrets", action="store_true", help="do not refuse files that look like credentials")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask before deploying to production")
    p = add("emulate", cmd_emulate, "Run your functions locally against the deployment")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--watch", action="store_true", help="reload when a source file changes")
    p.add_argument("--git-branch", action="store_true", help="act as the git branch's deployment")
    p.add_argument("--dir")
    p = add("invoke", cmd_invoke, "Call a function: the deployed one, or --local")
    p.add_argument("function")
    p.add_argument("-d", "--data", help="input as JSON, @file.json, or - for stdin")
    p.add_argument("--local", action="store_true", help="run your local copy (against the deployment's runtime)")
    p.add_argument("--as-user", help="run as this user id")
    p.add_argument("--role", action="append", help="a role for --as-user (repeatable)")
    p.add_argument("--permission", action="append", help="a permission for --as-user (repeatable)")
    p = add("trigger", cmd_trigger, "Fire an event, a schedule or a flow")
    p.add_argument("kind", choices=["event", "schedule", "flow"])
    p.add_argument("name")
    p.add_argument("-d", "--data", help="payload/input as JSON, @file.json, or -")
    p.add_argument("--remote", action="store_true", help="(event) just emit it on the deployment: only deployed code runs")
    p.add_argument("--local-code", action="store_true", help=argparse.SUPPRESS)
    add("functions", cmd_functions, "List deployed and local functions")
    add("deployments", cmd_deployments, "List deployments")
    p = add("rollback", cmd_rollback, "Re-activate an earlier deployment (default: the one before the active)")
    p.add_argument("deployment", nargs="?", help="a deployment id (a prefix is enough)")
    p = add("branches", cmd_branches, "List branches that have functions deployed")
    p.add_argument("--remove", metavar="BRANCH", help="delete a branch's functions")
    p = add("logs", cmd_logs, "Show recent function runs")
    p.add_argument("--function")
    p.add_argument("--status", choices=["succeeded", "failed"])
    p.add_argument("-n", "--limit", type=int, default=20)
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--run", metavar="ID", help="show one run in full, with its logs")
    p = add("test", cmd_test, "Run the project's tests (pytest)")
    p.add_argument("pytest_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for name, default in (("url", None), ("api_key", None), ("project", None), ("environment", None), ("branch", None), ("json", False), ("quiet", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    out = Out(as_json=args.json, quiet=args.quiet)
    try:
        return int(args.handler(args, out) or 0)
    except CliError as error:
        out.fail(str(error))
        return error.code
    except PawabaseError as error:
        out.fail(describe_error(error))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
