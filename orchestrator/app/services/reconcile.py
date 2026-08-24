"""The reconciliation loop.

Compares what the database says exists to what actually exists, in both
directions, and reports the difference. Acting on it is a separate, explicit
step -- `slipway reconcile --dry-run` reports, `--apply` acts.

Anything interruptible needs one of these. Sandboxes and deploys are created
after their identifier is recorded, so a crash mid-create leaves a row with no
resource; a crash mid-teardown leaves a resource with no live row.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

import structlog

from app.deploy.base import Deployer
from app.domain.entities import DeploymentStatus, RunState
from app.domain.repositories import UnitOfWork
from app.sandbox.base import Sandbox, SandboxHandle

log = structlog.get_logger(__name__)

UowFactory = Callable[[], UnitOfWork]


@dataclass(frozen=True, slots=True)
class Discrepancy:
    kind: str
    subject: str
    detail: str
    run_id: UUID | None = None
    deployment_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    orphan_sandboxes: list[Discrepancy] = field(default_factory=list)
    #: A container exists that no live record claims. Created behind our back,
    #: or left by a crash between `docker run` and the row being settled.
    orphan_deployments: list[Discrepancy] = field(default_factory=list)
    #: A record still holding its port whose container is gone -- removed by
    #: hand, or by a `docker system prune`. The port is being held for nothing.
    vanished_deployments: list[Discrepancy] = field(default_factory=list)

    @property
    def all(self) -> list[Discrepancy]:
        return [
            *self.orphan_sandboxes,
            *self.orphan_deployments,
            *self.vanished_deployments,
        ]

    @property
    def is_clean(self) -> bool:
        return not self.all


class Reconciler:
    def __init__(
        self,
        uow_factory: UowFactory,
        sandbox: Sandbox,
        deployer: Deployer,
        *,
        deploy_host: str,
        teardown_timeout_seconds: float,
    ) -> None:
        self._uow = uow_factory
        self._sandbox = sandbox
        self._deployer = deployer
        self._deploy_host = deploy_host
        self._teardown_timeout = teardown_timeout_seconds

    async def inspect(self) -> ReconcileReport:
        """Report differences. Changes nothing."""
        async with self._uow() as uow:
            # A sandbox belongs to a run that is still working. Once a run
            # reaches any terminal state -- deployed included -- its sandbox is
            # rubbish, because building is over.
            building_runs = {
                run.id
                for run in await uow.runs.list(states=_STILL_WORKING, limit=1000)
            }
            # A deployment and its port belong to a run that is working OR that
            # deployed successfully. A DEPLOYED run is terminal but its
            # deployment is the entire point of the run: it is serving a client.
            # Treating it as an orphan would have `reconcile --apply` tear down
            # every live application Slipway has ever shipped.
            entitled_runs = building_runs | {
                run.id
                for run in await uow.runs.list(
                    states=frozenset({RunState.DEPLOYED}), limit=1000
                )
            }

        live_sandboxes = await self._sandbox.list_live()
        orphan_sandboxes = [
            Discrepancy(
                kind="sandbox",
                subject=handle.name,
                detail="container exists but its run has finished building",
                run_id=handle.run_id,
            )
            for handle in live_sandboxes
            if handle.run_id not in building_runs
        ]

        # Both directions, against the store rather than against a guess.
        async with self._uow() as uow:
            records = await uow.deployments.list_holding_ports(host=self._deploy_host)
        by_id = {record.id: record for record in records}

        containers = await self._deployer.list_live()
        seen_ids = {container.deployment_id for container in containers}

        # Direction 1: a container with no live record claiming it.
        orphan_deployments = [
            Discrepancy(
                kind="deployment",
                subject=container.project_name,
                detail=(
                    "container exists but no live deployment record claims it"
                    if container.deployment_id not in by_id
                    else "container exists but its run failed, was cancelled, or is unknown"
                ),
                run_id=container.run_id,
                deployment_id=container.deployment_id,
            )
            for container in containers
            if container.deployment_id not in by_id or container.run_id not in entitled_runs
        ]

        # Direction 2: a record holding a port whose container is gone. The
        # record is the only thing keeping that port allocated.
        vanished_deployments = [
            Discrepancy(
                kind="deployment_record",
                subject=record.container_name or str(record.id),
                detail=(
                    f"record holds port {record.port} but its container no longer exists"
                ),
                run_id=record.run_id,
                deployment_id=record.id,
            )
            for record in records
            if record.status is DeploymentStatus.LIVE and record.id not in seen_ids
        ]

        return ReconcileReport(
            orphan_sandboxes=orphan_sandboxes,
            orphan_deployments=orphan_deployments,
            vanished_deployments=vanished_deployments,
        )

    async def apply(self, report: ReconcileReport) -> ReconcileReport:
        """Act on a report produced by `inspect`.

        Takes the report rather than re-deriving it, so that what an operator
        read in `--dry-run` is exactly what gets done.
        """
        for discrepancy in report.orphan_sandboxes:
            if discrepancy.run_id is None:
                continue
            await self._sandbox.stop(
                SandboxHandle(
                    run_id=discrepancy.run_id, name=discrepancy.subject, backend="reconciler"
                ),
                timeout_seconds=self._teardown_timeout,
            )
            log.info("reconcile.sandbox_removed", container=discrepancy.subject)

        for discrepancy in report.orphan_deployments:
            # Keyed on the deployment id, not the run id. An earlier version
            # passed run_id to teardown(), which addresses containers by
            # deployment id -- so it tore down nothing, or something else.
            if discrepancy.deployment_id is None:
                continue
            await self._deployer.teardown(
                discrepancy.deployment_id, timeout_seconds=self._teardown_timeout
            )
            log.info("reconcile.deployment_torn_down", project=discrepancy.subject)

        # Settle the record for anything torn down or already gone, in one pass.
        # `release_port=True` is what gives the port back, and it is the same
        # statement that records how the deployment ended -- so a reconciled
        # deployment cannot end up with an outcome and a port it still holds.
        settling = [*report.orphan_deployments, *report.vanished_deployments]
        if settling:
            async with self._uow() as uow:
                for discrepancy in settling:
                    if discrepancy.deployment_id is None:
                        continue
                    settled = await uow.deployments.settle(
                        discrepancy.deployment_id,
                        status=DeploymentStatus.TORN_DOWN,
                        log=f"reconciled: {discrepancy.detail}",
                        release_port=True,
                    )
                    if settled is not None:
                        log.info("reconcile.record_settled", subject=discrepancy.subject)
                await uow.commit()

        return await self.inspect()


#: Runs that still have work in flight. Anything outside this set has stopped,
#: which is what makes its sandbox collectable.
_STILL_WORKING = frozenset(
    state
    for state in RunState
    if state not in {RunState.DEPLOYED, RunState.FAILED, RunState.CANCELLED}
)
