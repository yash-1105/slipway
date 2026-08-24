"""deploy seam: publishing the built application.

V1 is `docker compose` over SSH to the one server, one compose project per run.
Ports are allocated through the database, never by scanning for a free one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class DeploymentTarget:
    """Where a deploy is going, and under what identity.

    Everything here is decided and recorded before the deployer is called, so a
    crash inside it leaves a row describing what should exist.
    """

    run_id: UUID
    deployment_id: UUID
    host: str
    project_name: str
    port: int
    #: Directory holding the project's own Dockerfile. The deployer builds what
    #: the project actually ships, not a wrapper around it.
    context_path: str = ""
    #: The port the application listens on inside the container.
    container_port: int = 3000
    #: Passed with `docker build --build-arg`, never as runtime environment.
    #: A framework that inlines a variable at build time gets nothing from one
    #: supplied at run time, and the resulting deploy is silently wrong.
    build_args: dict[str, str] = field(default_factory=dict)
    #: Supplied to the running container.
    runtime_env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Deployment:
    """A live deploy."""

    deployment_id: UUID
    run_id: UUID
    url: str
    project_name: str
    artifact_uri: str
    healthy: bool


@dataclass(frozen=True, slots=True)
class DeployFailure:
    reason: Literal[
        "build_failed",
        "start_failed",
        "unhealthy",
        "no_healthcheck",
        "unreachable",
        "timeout",
    ]
    detail: str
    #: The deploy log, which is the whole point of returning this as a value.
    log: str = ""
    deployment_id: UUID | None = None


DeployResult = Deployment | DeployFailure


#: Prefixes whose variables are inlined at build time by the frameworks this
#: system deploys. A value supplied at run time under one of these names has no
#: effect, because the build already happened -- and the result is a deploy that
#: succeeds and is silently wrong.
BUILD_ARG_PREFIXES = ("VITE_", "NEXT_PUBLIC_", "PUBLIC_", "REACT_APP_")


def is_build_time(name: str) -> bool:
    return name.startswith(BUILD_ARG_PREFIXES)


def split_env(env: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """(build args, runtime env) from one flat mapping.

    Callers hand over the application's variables without having to know which
    framework inlines what. Lives on the seam rather than in an implementation
    because `DeploymentTarget` carries both halves: the split is part of the
    contract, not a detail of how one backend happens to work.
    """
    build = {k: v for k, v in env.items() if is_build_time(k)}
    runtime = {k: v for k, v in env.items() if not is_build_time(k)}
    return build, runtime


def project_name_for(deployment_id: UUID) -> str:
    """The deployment's name, derived from its id.

    Deterministic, so it can be written down before anything exists and a retry
    addresses the same thing rather than creating a second one.
    """
    return f"slipway-preview-{deployment_id}"


class Deployer(Protocol):
    """The deploy seam.

    Infrastructure only: it builds, starts, stops and lists. It holds no store,
    which is why there is no `rollback` here -- rolling back means re-deploying
    a *recorded* target, and a component with no records cannot do it. That
    belongs to a service. An earlier version of this Protocol did declare
    `rollback`, and the only implementation of it returned a failure value
    explaining that it could not be implemented; the declaration was the bug.
    """

    async def deploy(
        self, target: DeploymentTarget, artifact_uri: str, *, timeout_seconds: float
    ) -> DeployResult:
        """Bring up the artifact at the target. Idempotent per deployment_id."""

    async def teardown(self, deployment_id: UUID, *, timeout_seconds: float) -> None:
        """Idempotent. Tearing down something already gone is success."""

    async def list_live(self) -> list[Deployment]:
        """Every deployment this backend can still see, running or stopped.

        Reconciliation needs both: a stopped container is still holding a name
        and still needs removing, and a container that has vanished is what
        tells us a record is stale.
        """
