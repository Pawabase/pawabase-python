"""Functions: what is deployed, deployments and rollbacks, branches, runs, source, and running one as an operator."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._base import Service, seg


class Functions(Service):
    """Deployed functions (Python or TypeScript; a project's functions are one language)."""

    def list(self, *, branch: str | None = None) -> Any:
        """Functions visible to the environment (and *branch*), each saying whether it came from a branch, a deployment or mounted code."""
        return self.get("/functions", branch=branch)

    def run(self, name: str, input: Any = None, *, branch: str | None = None, as_user: Mapping[str, Any] | None = None) -> Any:
        """Run a function now as an operator (or ``as_user``), bypassing its HTTP policy: ``{status, result | error}``."""
        return self.post(seg("functions", name, "invoke"), {"input": input, "branch": branch, "as_user": dict(as_user) if as_user else None})

    # deployments
    def deploy(self, archive: str, *, branch: str = "main", manifest: Mapping[str, Any] | None = None, runtime: str = "python3.11", limits: Mapping[str, Any] | None = None) -> Any:
        """Upload a base64 ``.tar.gz`` bundle (``functions/*.py`` or ``functions/*.ts``) and activate it. The ``pawabase deploy`` command builds the bundle."""
        return self.post("/function-deployments", {"archive": archive, "branch": branch, "manifest": dict(manifest or {}), "runtime": runtime, "limits": dict(limits or {})})

    def deployments(self, *, branch: str | None = None, limit: int = 50) -> Any:
        return self.get("/function-deployments", branch=branch, limit=limit)

    def deployment(self, deployment_id: str) -> Any:
        return self.get(seg("function-deployments", deployment_id))

    def activate(self, deployment_id: str) -> Any:
        """Make an earlier deployment the active one again (a rollback)."""
        return self.post(seg("function-deployments", deployment_id, "activate"))

    def remove_deployment(self, deployment_id: str) -> Any:
        return self.delete(seg("function-deployments", deployment_id))

    # branches
    def branches(self) -> Any:
        return self.get("/function-branches")

    def remove_branch(self, branch: str) -> Any:
        """Delete the functions deployed on *branch*. The environment's own are untouched."""
        return self.delete(seg("function-branches", branch))

    # runs and source
    def runs(self, function: str | None = None, *, branch: str | None = None, status: str | None = None, after: str | None = None, limit: int = 50) -> Any:
        return self.get("/function-runs", function=function, branch=branch, status=status, after=after, limit=limit)

    def run_detail(self, run_id: str) -> Any:
        """One run with its input, output, error and logs."""
        return self.get(seg("function-runs", run_id))

    def source(self, *, branch: str | None = None) -> Any:
        """The files of the live deployment."""
        return self.get("/function-source", branch=branch)

    def source_file(self, path: str, *, branch: str | None = None) -> Any:
        return self.get("/function-source/file", path=path, branch=branch)
