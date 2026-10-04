"""Where the CLI and the emulator find their settings.

Four things say which Pawabase to talk to: the gateway **URL**, an **API key**, and the **environment** it belongs to (plus a **branch**, when you
work on one). They come from, in order of priority:

1. command-line flags (``--url``, ``--api-key``, ``--environment``, ``--branch``);
2. environment variables (``PAWABASE_URL``, ``PAWABASE_API_KEY``, ``PAWABASE_ENVIRONMENT``, ``PAWABASE_BRANCH``);
3. a ``.env`` file next to the project file (so a deploy key lives with the project and stays out of version control);
4. ``pawabase.toml`` in the project (never the key): ``url``, ``environment``, ``branch``, ``functions``, ``include``, ``exclude``, limits;
5. the file ``pawabase login`` wrote to your home directory (``url`` and ``api_key``, mode 0600).

The project file is found by walking up from the current directory, like ``git`` finds ``.git``. An older ``pawabase.json`` is read too.

The API key is a *secret* key of the environment (``pb_sk_…``): deploying and emulating are management actions. It is never written into the project file, and ``repr`` of
the settings hides it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

PROJECT_FILES = ("pawabase.toml", "pawabase.json")
GLOBAL_FILE = Path(os.environ.get("PAWABASE_CONFIG_HOME", Path.home() / ".config" / "pawabase")) / "config.json"
ENV_NAMES = {"url": "PAWABASE_URL", "api_key": "PAWABASE_API_KEY", "environment": "PAWABASE_ENVIRONMENT", "branch": "PAWABASE_BRANCH"}
BRANCH_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")


class ConfigError(Exception):
    """Something needed is missing or wrong. The message says what to do about it."""


@dataclass(frozen=True)
class Settings:
    """Everything the CLI needs, with where the project lives."""

    url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    environment: str = "development"
    branch: str = "main"
    root: Path = field(default_factory=Path.cwd)
    functions: str = "functions"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    timeout: int = 30
    memory_mb: int = 256
    sources: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:  # the key must never reach a log or a traceback
        key = "set" if self.api_key else "missing"
        return f"Settings(url={self.url!r}, environment={self.environment!r}, branch={self.branch!r}, api_key=<{key}>)"

    @property
    def functions_dir(self) -> Path:
        return (self.root / self.functions).resolve()

    def require(self, *names: str) -> Settings:
        """Raise :class:`ConfigError` naming what is missing, and the way to supply each."""
        hints = {
            "url": "--url, PAWABASE_URL, or `pawabase login --url …`",
            "api_key": "--api-key, PAWABASE_API_KEY (in .env), or `pawabase login`",
        }
        missing = [name for name in names if not getattr(self, name)]
        if missing:
            raise ConfigError("Missing " + "; ".join(f"{name.replace('_', ' ')} ({hints[name]})" for name in missing))
        return self

    def describe(self) -> dict[str, Any]:
        """Settings and where each came from, for ``pawabase whoami`` (the key shown as a hint, never whole)."""
        key = self.api_key
        return {
            "url": self.url, "environment": self.environment, "branch": self.branch,
            "api_key": f"{key[:8]}…{key[-4:]}" if key and len(key) > 14 else ("set" if key else None),
            "root": str(self.root), "functions": self.functions, "sources": dict(self.sources),
        }


# ── reading ──────────────────────────────────────────────────────────────


def slugify_branch(name: str) -> str:
    """A git branch name as a Pawabase branch name: ``Feature/Login Page`` → ``feature-login-page``."""
    text = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not text:
        return "main"
    return text if text[0].isalpha() else f"b-{text}"[:63]


def git_branch(directory: Path) -> str | None:
    """The checked-out git branch, or ``None`` outside a repository or on a detached head."""
    try:
        out = subprocess.run(["git", "-C", str(directory), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    name = out.stdout.strip()
    return name if out.returncode == 0 and name and name != "HEAD" else None


def git_commit(directory: Path) -> dict[str, Any]:
    """The commit being deployed, for the manifest: ``{"commit", "dirty", "message"}`` (empty outside git)."""
    try:
        sha = subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=False)
        if sha.returncode != 0:
            return {}
        status = subprocess.run(["git", "-C", str(directory), "status", "--porcelain"], capture_output=True, text=True, timeout=5, check=False)
        subject = subprocess.run(["git", "-C", str(directory), "log", "-1", "--pretty=%s"], capture_output=True, text=True, timeout=5, check=False)
        return {"commit": sha.stdout.strip(), "dirty": bool(status.stdout.strip()), "message": subject.stdout.strip()[:200]}
    except (OSError, subprocess.SubprocessError):
        return {}


def find_project_file(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        for name in PROJECT_FILES:
            if (directory / name).is_file():
                return directory / name
    return None


def parse_dotenv(text: str) -> dict[str, str]:
    """``KEY=value`` lines, with ``#`` comments, ``export``, and single or double quotes. Nothing is expanded or executed."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            continue
        value = value.strip()
        if value[:1] in "\"'" and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value)
        out[key.strip()] = value
    return out


def _read_file(path: Path) -> dict[str, Any]:
    try:
        if path.suffix == ".toml":
            return tomllib.loads(path.read_text())
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (ValueError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"{path} is not valid: {error}") from error


def load(
    *,
    cwd: Path | None = None,
    url: str | None = None,
    api_key: str | None = None,
    environment: str | None = None,
    branch: str | None = None,
    env: Mapping[str, str] | None = None,
    use_git_branch: bool = False,
) -> Settings:
    """Resolve settings from every source. ``use_git_branch`` takes the branch from the checked-out git branch when nothing else names one."""
    cwd = (cwd or Path.cwd()).resolve()
    environ = dict(os.environ if env is None else env)
    project_file = find_project_file(cwd)
    root = project_file.parent if project_file else cwd
    values: dict[str, Any] = {}
    origin: dict[str, str] = {}

    def apply(source: str, data: Mapping[str, Any]) -> None:
        for name in ("url", "api_key", "environment", "branch", "functions", "timeout", "memory_mb"):
            if data.get(name) not in (None, ""):
                values[name] = data[name]
                origin[name] = source
        for name in ("include", "exclude", "paths"):
            if data.get(name):
                values[name] = tuple(str(item) for item in data[name])
                origin[name] = source

    apply("~/.config/pawabase/config.json", _read_file(GLOBAL_FILE))
    if project_file:
        file_data = _read_file(project_file)
        if "api_key" in file_data:
            raise ConfigError(f"{project_file.name} contains an api_key. Keys do not belong in a file that is committed: put it in .env as PAWABASE_API_KEY and remove it here.")
        apply(project_file.name, file_data)
    dotenv = parse_dotenv((root / ".env").read_text()) if (root / ".env").is_file() else {}
    apply(".env", {name: dotenv.get(var) for name, var in ENV_NAMES.items()})
    apply("environment", {name: environ.get(var) for name, var in ENV_NAMES.items()})
    apply("flag", {"url": url, "api_key": api_key, "environment": environment, "branch": branch})

    resolved_branch = str(values.get("branch") or "")
    if not resolved_branch and use_git_branch:
        guessed = git_branch(root)
        if guessed:
            resolved_branch = slugify_branch(guessed)
            origin["branch"] = "git"
    resolved_branch = resolved_branch or "main"
    if not BRANCH_PATTERN.match(resolved_branch):
        raise ConfigError(f"{resolved_branch!r} is not a valid branch name (lowercase letters, digits, - and _, starting with a letter). `{slugify_branch(resolved_branch)}` would do.")
    gateway = str(values["url"]).rstrip("/") if values.get("url") else None
    if gateway and not gateway.startswith(("http://", "https://")):
        raise ConfigError(f"The url must start with http:// or https:// (got {gateway!r}).")
    return Settings(
        url=gateway,
        api_key=values.get("api_key"),
        environment=str(values.get("environment") or "development"),
        branch=resolved_branch,
        root=root,
        functions=str(values.get("functions") or "functions"),
        include=values.get("include", ()),
        exclude=values.get("exclude", ()),
        paths=values.get("paths", ()),
        timeout=int(values.get("timeout") or 30),
        memory_mb=int(values.get("memory_mb") or 256),
        sources=origin,
    )


# ── writing ──────────────────────────────────────────────────────────────


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(str(value))


def write_project_file(root: Path, data: Mapping[str, Any]) -> Path:
    """Write ``pawabase.toml`` (never a key). Existing keys not mentioned in *data* are kept."""
    path = root / "pawabase.toml"
    current: dict[str, Any] = {}
    if path.is_file():
        current = tomllib.loads(path.read_text())
    elif (root / "pawabase.json").is_file():
        current = json.loads((root / "pawabase.json").read_text())
    current.update({k: v for k, v in data.items() if v not in (None, "")})
    current.pop("api_key", None)
    lines = ["# Which Pawabase this project deploys to. The API key is not here: it belongs in .env (PAWABASE_API_KEY).", ""]
    lines += [f"{key} = {_toml_value(value)}" for key, value in current.items()]
    path.write_text("\n".join(lines) + "\n")
    return path


def write_global(data: Mapping[str, Any]) -> Path:
    """Remember a URL and key for this machine (mode 0600)."""
    GLOBAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    current = _read_file(GLOBAL_FILE)
    current.update({k: v for k, v in data.items() if v not in (None, "")})
    GLOBAL_FILE.write_text(json.dumps(current, indent=2) + "\n")
    os.chmod(GLOBAL_FILE, 0o600)
    return GLOBAL_FILE


def with_overrides(settings: Settings, **changes: Any) -> Settings:
    return replace(settings, **changes)
