"""deploy seam: an in-memory deployer for tests/unit and `make dev`."""

from __future__ import annotations

from uuid import UUID

from app.deploy.base import (
    Deployer,
    DeployFailure,
    Deployment,
    DeploymentTarget,
    DeployResult,
)


class FakeDeployer(Deployer):
    def __init__(self, *, fail_with: DeployFailure | None = None) -> None:
        self._fail_with = fail_with
        self._live: dict[UUID, Deployment] = {}

    async def deploy(
        self, target: DeploymentTarget, artifact_uri: str, *, timeout_seconds: float
    ) -> DeployResult:
        if self._fail_with is not None:
            return self._fail_with
        deployment = Deployment(
            deployment_id=target.deployment_id,
            run_id=target.run_id,
            url=f"http://{target.host}:{target.port}",
            project_name=target.project_name,
            artifact_uri=artifact_uri,
            healthy=True,
        )
        self._live[target.deployment_id] = deployment
        return deployment

    async def teardown(self, deployment_id: UUID, *, timeout_seconds: float) -> None:
        self._live.pop(deployment_id, None)

    async def list_live(self) -> list[Deployment]:
        return list(self._live.values())
