"""Deploying a preview: allocate, record, build, settle.

The deployer is infrastructure and holds no store. This is the half that does:
it decides which port a deployment gets, writes the row down before anything
exists, and records how it turned out.

The order matters more than anything else here. The row is written first, with
the container's name in it, because a crash between the write and `docker run`
leaves a row naming something that does not exist -- which the reconciler can
repair. The reverse, a container with no row, is the one case that cannot be.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import structlog

from app.deploy.base import (
    Deployer,
    DeployFailure,
    DeploymentTarget,
    DeployResult,
    project_name_for,
    split_env,
)
from app.domain.entities import DeploymentRecord, DeploymentStatus
from app.domain.errors import ResourceExhaustedError
from app.domain.ids import uuid7
from app.domain.repositories import UnitOfWork

log = structlog.get_logger(__name__)

UowFactory = Callable[[], UnitOfWork]


class DeployService:
    def __init__(
        self,
        uow_factory: UowFactory,
        deployer: Deployer,
        *,
        host: str,
        port_range: tuple[int, int],
        timeout_seconds: float,
        network: str = "slipway-previews",
    ) -> None:
        self._network = network
        self._uow = uow_factory
        self._deployer = deployer
        self._host = host
        self._port_start, self._port_end = port_range
        self._timeout = timeout_seconds

    async def allocate(
        self,
        run_id: UUID,
        *,
        artifact_uri: str,
        image_tag: str | None = None,
        container_port: int = 3000,
    ) -> DeploymentRecord:
        """Claim a port by inserting, and record the deployment before it exists.

        Walks the range upward, letting the unique index arbitrate. Nothing here
        asks the operating system what looks free: two workers asking that at
        the same moment get the same answer, and both then try to bind it.
        """
        deployment_id = uuid7()
        name = project_name_for(deployment_id)

        async with self._uow() as uow:
            for port in range(self._port_start, self._port_end + 1):
                candidate = DeploymentRecord(
                    id=deployment_id,
                    run_id=run_id,
                    status=DeploymentStatus.RECORDING,
                    host=self._host,
                    port=port,
                    project_name=name,
                    artifact_uri=artifact_uri,
                    created_at=datetime.now(UTC),
                    container_name=name,
                    image_tag=image_tag,
                    container_port=container_port,
                    network=self._network,
                )
                claimed = await uow.deployments.claim_port(candidate)
                if claimed is not None:
                    await uow.commit()
                    log.info(
                        "deploy.port_claimed",
                        deployment_id=str(deployment_id),
                        run_id=str(run_id),
                        port=port,
                    )
                    return claimed
                # Another deployment holds this port. Normal; try the next.

            await uow.rollback()

        raise ResourceExhaustedError(
            f"no free port on {self._host} in range {self._port_start}-{self._port_end}; "
            "release stale deployments with `slipway reconcile --apply`"
        )

    async def deploy(
        self,
        run_id: UUID,
        *,
        context_path: str,
        artifact_uri: str,
        env: dict[str, str] | None = None,
        container_port: int = 3000,
    ) -> DeployResult:
        """Allocate, record, build and start. Failures come back as values."""
        build_args, runtime_env = split_env(env or {})
        record = await self.allocate(
            run_id, artifact_uri=artifact_uri, container_port=container_port
        )

        target = DeploymentTarget(
            run_id=run_id,
            deployment_id=record.id,
            host=self._host,
            project_name=record.project_name,
            port=record.port,
            context_path=context_path,
            container_port=container_port,
            build_args=build_args,
            runtime_env=runtime_env,
        )

        result = await self._deployer.deploy(
            target, artifact_uri, timeout_seconds=self._timeout
        )

        if isinstance(result, DeployFailure):
            # Releasing the port and recording the failure are one statement, so
            # a failed deploy cannot leave its port held.
            await self._settle(
                record.id,
                status=DeploymentStatus.FAILED,
                detail=f"{result.reason}: {result.detail}\n\n{result.log}".strip(),
                release_port=True,
            )
            log.warning(
                "deploy.failed",
                deployment_id=str(record.id),
                reason=result.reason,
                detail=result.detail[:300],
            )
            return result

        await self._settle(
            record.id,
            status=DeploymentStatus.LIVE,
            url=result.url,
            container_id=result.artifact_uri or None,
        )
        log.info("deploy.live", deployment_id=str(record.id), url=result.url)
        return result

    async def destroy(self, deployment_id: UUID) -> None:
        """Remove the container and give the port back. Idempotent.

        Safe to call on a deployment that was never started, one already
        destroyed, and one whose container a human removed by hand.
        """
        await self._deployer.teardown(deployment_id, timeout_seconds=self._timeout)
        await self._settle(
            deployment_id,
            status=DeploymentStatus.TORN_DOWN,
            release_port=True,
        )
        log.info("deploy.destroyed", deployment_id=str(deployment_id))

    async def list_holding_ports(self) -> list[DeploymentRecord]:
        async with self._uow() as uow:
            return await uow.deployments.list_holding_ports(host=self._host)

    async def get(self, deployment_id: UUID) -> DeploymentRecord | None:
        async with self._uow() as uow:
            return await uow.deployments.get(deployment_id)

    async def _settle(
        self,
        deployment_id: UUID,
        *,
        status: DeploymentStatus,
        url: str | None = None,
        container_id: str | None = None,
        detail: str | None = None,
        release_port: bool = False,
    ) -> None:
        async with self._uow() as uow:
            await uow.deployments.settle(
                deployment_id,
                status=status,
                url=url,
                container_id=container_id,
                log=detail,
                release_port=release_port,
            )
            await uow.commit()
