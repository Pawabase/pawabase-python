"""Packing a project's functions into the archive ``pawabase deploy`` uploads.

The archive is a gzip tar with this layout::

    functions/*.py            your functions
    <package>/...             each ``include`` entry (helper packages the functions import), at the archive root by its own name

It is **reproducible**: entries are sorted, timestamps, owners and modes are normalised, and the gzip header carries no clock, so the same source always gives the
same bytes and the same checksum. ``pawabase deploy`` uses that to tell you "nothing changed" instead of activating an identical deployment.

It is **conservative about what leaves your machine**: caches, dotfiles, tests and virtualenvs are left out; a file that looks like a credential (``.env``, a private key, a
live API key inside source) stops the deploy with the file and line named, rather than being uploaded quietly.
"""

from __future__ import annotations

import base64
import fnmatch
import gzip
import hashlib
import io
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_FILES = 2_000
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024

#: Never part of a bundle, however it is configured.
ALWAYS_EXCLUDED = ("__pycache__", "*.pyc", "*.pyo", ".*", "node_modules", "*.egg-info", ".venv", "venv", "dist", "build")
#: Left out unless an ``include`` names them explicitly: a deployed function does not need its own tests.
DEFAULT_EXCLUDED = ("tests", "test", "test_*.py", "*_test.py", "conftest.py")
#: Files that are credentials by their name.
SECRET_FILES = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "credentials.json", "*.keystore")
SECRET_PATTERNS = (
    (re.compile(r"\bpb_sk_[A-Za-z0-9_-]{20,}"), "a Pawabase secret key"),
    (re.compile(r"\bsk_live_[A-Za-z0-9]{16,}"), "a live payment secret key"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"), "a private key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key id"),
    (re.compile(r"\bghp_[A-Za-z0-9]{30,}"), "a GitHub token"),
)


class BundleError(Exception):
    """The source cannot be bundled. ``problems`` lists each reason."""

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message if not problems else message + "\n  - " + "\n  - ".join(problems))
        self.problems = problems or []


@dataclass
class Bundle:
    archive: bytes
    checksum: str
    files: list[tuple[str, int]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def encoded(self) -> str:
        return base64.b64encode(self.archive).decode()

    @property
    def size(self) -> int:
        return len(self.archive)

    @property
    def total_bytes(self) -> int:
        return sum(size for _, size in self.files)


def _is_secret_file(name: str) -> bool:
    """A credential by its name (``.env``, a key file), not a ``.env.example`` that documents what to set."""
    return _matches(name, SECRET_FILES) and not name.endswith((".example", ".sample", ".template"))


def _matches(name: str, patterns: tuple[str, ...] | list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _walk(source: Path, prefix: str, exclude: list[str], skipped: list[str], *, explicit: bool) -> list[tuple[str, Path]]:
    """Every file under *source* as ``(archive path, real path)``, skipping what is excluded."""
    found: list[tuple[str, Path]] = []
    if source.is_file():
        return [(f"{prefix}".rstrip("/") or source.name, source)]
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        parts = relative.parts
        if any(_matches(part, ALWAYS_EXCLUDED) and not _is_secret_file(part) for part in parts):
            if path.is_file() and not any(part == "__pycache__" or part.endswith(".pyc") for part in parts):
                skipped.append(f"{prefix}{relative.as_posix()} (hidden file)")
            continue
        if not explicit and any(_matches(part, DEFAULT_EXCLUDED) for part in parts):
            if path.is_file():
                skipped.append(f"{prefix}{relative.as_posix()} (test file)")
            continue
        if any(_matches(relative.as_posix(), [pattern]) or _matches(parts[-1], [pattern]) for pattern in exclude):
            if path.is_file():
                skipped.append(f"{prefix}{relative.as_posix()} (excluded)")
            continue
        if path.is_symlink():
            skipped.append(f"{prefix}{relative.as_posix()} (symbolic link)")
            continue
        if path.is_file():
            found.append((f"{prefix}{relative.as_posix()}", path))
    return found


def _scan(entries: list[tuple[str, Path]]) -> list[str]:
    problems: list[str] = []
    for archive_path, real in entries:
        if _is_secret_file(real.name):
            problems.append(f"{archive_path} looks like a credential file. Keep it out of the bundle (functions read secrets with `await ctx.runtime.secret(name)`).")
            continue
        if real.suffix not in (".py", ".json", ".toml", ".yaml", ".yml", ".txt", ".cfg", ".ini", ".md", ".html", ".js", ".sql"):
            continue
        try:
            text = real.read_text(errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for pattern, what in SECRET_PATTERNS:
                if pattern.search(line):
                    problems.append(f"{archive_path}:{number} contains {what}. Move it to a secret (`pawabase secrets set`) or pass `--allow-secrets` if this is a test fixture.")
    return problems


def build(
    root: Path,
    *,
    functions: str = "functions",
    include: tuple[str, ...] | list[str] = (),
    exclude: tuple[str, ...] | list[str] = (),
    allow_secrets: bool = False,
) -> Bundle:
    """Pack ``<root>/<functions>`` and each ``include`` path into a reproducible archive."""
    root = root.resolve()
    functions_dir = (root / functions).resolve()
    if not functions_dir.is_dir():
        raise BundleError(f"There is no {functions}/ folder in {root}. Run `pawabase init` to create one, or set `functions = \"…\"` in pawabase.toml.")
    if not any(functions_dir.glob("*.py")):
        raise BundleError(f"{functions_dir} contains no .py files, so there is nothing to deploy.")
    skipped: list[str] = []
    entries = _walk(functions_dir, "functions/", list(exclude), skipped, explicit=False)
    seen_roots = {"functions"}
    for item in include:
        source = (root / item).resolve()
        if not source.exists():
            raise BundleError(f"include {item!r} does not exist (looked for {source}).")
        name = source.name
        if name in seen_roots:
            raise BundleError(f"include {item!r} would be placed at {name}/, which is already taken.")
        seen_roots.add(name)
        entries += _walk(source, f"{name}/", list(exclude), skipped, explicit=True)
    problems = [] if allow_secrets else _scan(entries)
    if len(entries) > MAX_FILES:
        problems.append(f"The bundle would contain {len(entries)} files; the limit is {MAX_FILES}. Exclude what the functions do not import.")
    sizes = [(archive_path, real.stat().st_size) for archive_path, real in entries]
    for archive_path, size in sizes:
        if size > MAX_FILE_BYTES:
            problems.append(f"{archive_path} is {size // 1024 // 1024}MB; the limit per file is {MAX_FILE_BYTES // 1024 // 1024}MB.")
    if sum(size for _, size in sizes) > MAX_TOTAL_BYTES:
        problems.append(f"The bundle is larger than {MAX_TOTAL_BYTES // 1024 // 1024}MB unpacked.")
    if problems:
        raise BundleError("The bundle cannot be built:", problems)

    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, compresslevel=9) as compressed, tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for archive_path, real in sorted(entries):
            data = real.read_bytes()
            info = tarfile.TarInfo(archive_path)
            info.size, info.mtime, info.uid, info.gid, info.uname, info.gname = len(data), 0, 0, 0, "", ""
            info.mode = 0o755 if real.stat().st_mode & 0o111 else 0o644
            archive.addfile(info, io.BytesIO(data))
    data = raw.getvalue()
    return Bundle(archive=data, checksum=hashlib.sha256(data).hexdigest(), files=[(p, s) for p, s in sorted(sizes)], skipped=sorted(skipped))


def read(archive: bytes) -> dict[str, bytes]:
    """The files in a bundle, for inspection and tests."""
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        for member in bundle.getmembers():
            handle = bundle.extractfile(member) if member.isfile() else None
            if handle is not None:
                out[member.name] = handle.read()
    return out


def manifest(settings_branch: str, git: dict[str, Any], functions: list[dict[str, Any]], bundle: Bundle, kit_version: str) -> dict[str, Any]:
    """What the deployment records about itself."""
    return {
        "kit": kit_version,
        "branch": settings_branch,
        "git": git,
        "files": len(bundle.files),
        "bytes": bundle.total_bytes,
        "functions": functions,
    }
