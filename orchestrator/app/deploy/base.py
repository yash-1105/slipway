"""deploy seam: publishing the built application.

V1 is `docker compose` over SSH to the one server, one compose project per run.
Ports are allocated through the database, never by scanning for a free one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class DeploymentTarget:
    """Where a deploy is going, and under what identity."""

    run_id: UUID
    deployment_id: UUID
    host: str
    project_name: str
    port: int


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
    reason: Literal["build_failed", "start_failed", "unhealthy", "unreachable", "timeout"]
    detail: str
    #: The deploy log, which is the whole point of returning this as a value.
    log: str = ""
    deployment_id: UUID | None = None


DeployResult = Deployment | DeployFailure


class Deployer(Protocol):
    """The deploy seam."""

    async def deploy(
        self, target: DeploymentTarget, artifact_uri: str, *, timeout_seconds: float
    ) -> DeployResult:
        """Bring up the artifact at the target. Idempotent per deployment_id."""

    async def rollback(
        self, run_id: UUID, to_deployment_id: UUID, *, timeout_seconds: float
    ) -> DeployResult:
        """Re-deploy a recorded artifact. Never rebuilds, never re-runs agents."""

    async def teardown(self, deployment_id: UUID, *, timeout_seconds: float) -> None:
        """Idempotent. Tearing down something already gone is success."""

    async def list_live(self) -> list[Deployment]:
        """What is actually running, for reconciliation."""
