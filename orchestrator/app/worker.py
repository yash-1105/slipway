"""The worker.

Claims one job at a time under a lease, runs it, and reports the outcome. Every
job body is idempotent, because a lease can expire mid-flight and another
worker will pick the same job up.

Shutdown is cooperative: SIGTERM stops the claim loop and lets the job in hand
finish or lose its lease honestly, rather than leaving a half-done deploy.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime
from uuid import UUID

import structlog

from app.config import Settings, get_settings
from app.container import Container, build_container
from app.domain.entities import Job, Trigger
from app.domain.errors import IllegalTransitionError, SlipwayError
from app.logging import configure_logging
from app.runtimes.base import GraphInvocation, GraphOutcome
from app.services.runs import RunService

log = structlog.get_logger(__name__)

#: Which graph a job kind runs. `deploy` is not a graph; it is handled below.
GRAPH_FOR_KIND = {"specify": "specify", "build": "build", "test": "test"}

#: The only artifact kind the deploy stage will deploy. Nothing produces one
#: yet -- the build stage emits `build_log`, which is an agent's prose about a
#: build rather than a build -- so every deploy currently fails here, loudly and
#: by name. That is the intended behaviour until a builder agent exists.
DEPLOYABLE_ARTIFACT_KIND = "bundle"


class Worker:
    def __init__(self, container: Container) -> None:
        self._c = container
        self._settings = container.settings
        self._stopping = asyncio.Event()

    def request_stop(self) -> None:
        log.info("worker.stop_requested", worker=self._settings.effective_worker_id)
        self._stopping.set()

    async def run_forever(self) -> None:
        kinds = frozenset(self._settings.worker_kinds)
        log.info(
            "worker.started",
            worker=self._settings.effective_worker_id,
            kinds=sorted(kinds),
            lease_seconds=self._settings.job_lease_seconds,
        )

        while not self._stopping.is_set():
            job = await self._c.jobs.claim(kinds)
            if job is None:
                await self._sleep_or_stop(self._settings.worker_poll_seconds)
                continue

            await self._run_job(job)

        log.info("worker.stopped", worker=self._settings.effective_worker_id)

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass  # the normal path: nothing asked us to stop

    async def _run_job(self, job: Job) -> None:
        structlog.contextvars.bind_contextvars(job_id=str(job.id), run_id=str(job.run_id))
        try:
            if job.kind == "reconcile":
                await self._reconcile(job)
                return

            graph = GRAPH_FOR_KIND.get(job.kind)
            if graph is not None:
                await self._run_graph_job(job, graph)
                return

            if job.kind == "deploy":
                await self._deploy(job)
                return

            # An unknown kind is a deployment mistake -- a new job kind shipped
            # to a worker that predates it. Abandon it rather than retrying a
            # job nothing here can ever run.
            await self._c.jobs.fail(job, f"unknown job kind {job.kind!r}")
        finally:
            structlog.contextvars.clear_contextvars()

    async def _run_graph_job(self, job: Job, graph: str) -> None:
        run = await self._c.runs.get(job.run_id)
        result = await self._c.runtime.invoke(
            GraphInvocation(
                run_id=job.run_id,
                graph=graph,
                inputs={"run_id": str(job.run_id), "brief": run.brief},
                idempotency_key=job.idempotency_key,
            ),
            timeout_seconds=self._settings.model_timeout_seconds * 4,
        )

        if isinstance(result, GraphOutcome) and not result.outputs.get("failure"):
            await self._store_outputs(job, result)
            await self._c.jobs.succeed(job)
            await self._advance(self._c.runs, job.run_id, Trigger.AGENT_SUCCEEDED, detail=graph)
            return

        detail = (
            str(result.outputs.get("failure"))
            if isinstance(result, GraphOutcome)
            else f"{result.reason}: {result.detail}"
        )
        will_retry = await self._c.jobs.fail(job, detail)
        if not will_retry:
            await self._advance(self._c.runs, job.run_id, Trigger.AGENT_FAILED, detail=detail)

    async def _store_outputs(self, job: Job, result: GraphOutcome) -> None:
        from app.artifacts.base import ArtifactFailure
        from app.domain.entities import ArtifactRef
        from app.domain.ids import uuid7

        for key in ("spec", "build_log", "test_report"):
            value = result.outputs.get(key)
            if not isinstance(value, str) or not value:
                continue

            stored = await self._c.artifacts.put(
                job.run_id,
                key,
                value.encode(),
                timeout_seconds=self._settings.artifact_timeout_seconds,
            )
            if isinstance(stored, ArtifactFailure):
                log.warning("artifact.store_failed", kind=key, reason=stored.reason)
                continue

            # Recorded after the bytes are durable, so a row never points at
            # something that was never written.
            async with self._c.uow() as uow:
                await uow.artifacts.record(
                    ArtifactRef(
                        id=uuid7(),
                        run_id=job.run_id,
                        kind=key,
                        uri=stored.uri,
                        sha256=stored.sha256,
                        size_bytes=stored.size_bytes,
                        created_at=datetime.now(UTC),
                    )
                )
                await uow.commit()

    async def _deploy(self, job: Job) -> None:
        from app.deploy.base import DeployFailure, DeploymentTarget
        from app.domain.ids import uuid7

        settings = self._settings
        host = settings.deploy_public_host

        # Before anything is allocated. Deploying whatever artifact happens to
        # exist produced a URL that served nothing: the previous version fell
        # back to the `build_log`, which is an agent's prose about a build, not
        # a build.
        artifact_uri = await self._deployable_artifact_uri(job.run_id)
        if artifact_uri is None:
            detail = (
                f"no deployable artifact for run {job.run_id}: nothing has "
                f"produced an artifact of kind {DEPLOYABLE_ARTIFACT_KIND!r}. "
                "The build stage currently emits a log, not a bundle, so there "
                "is nothing to deploy. Refusing rather than deploying a "
                "placeholder."
            )
            log.error("deploy.no_artifact", run_id=str(job.run_id), kind=DEPLOYABLE_ARTIFACT_KIND)
            # Terminal: a retry cannot conjure an artifact no stage produces.
            await self._c.jobs.fail(job, detail, terminal=True)
            await self._advance(self._c.runs, job.run_id, Trigger.DEPLOY_FAILED, detail=detail)
            return

        allocation = await self._c.ports.allocate(job.run_id, host=host)
        deployment_id = uuid7()

        # The identifier exists in our world before it exists on the server, so
        # a crash inside `deploy` leaves something the reconciler can find.
        target = DeploymentTarget(
            run_id=job.run_id,
            deployment_id=deployment_id,
            host=host,
            project_name=f"slipway-{deployment_id}",
            port=allocation.port,
        )

        result = await self._c.deployer.deploy(
            target, artifact_uri, timeout_seconds=settings.deploy_timeout_seconds
        )

        if isinstance(result, DeployFailure):
            await self._c.ports.release(allocation.id)
            will_retry = await self._c.jobs.fail(job, f"{result.reason}: {result.detail}")
            if not will_retry:
                await self._advance(
                    self._c.runs,
                    job.run_id,
                    Trigger.DEPLOY_FAILED,
                    detail=f"{result.reason}: {result.detail}",
                )
            return

        await self._c.jobs.succeed(job)
        await self._advance(self._c.runs, job.run_id, Trigger.DEPLOY_SUCCEEDED, detail=result.url)

    async def _deployable_artifact_uri(self, run_id: UUID) -> str | None:
        """The newest deployable bundle for a run, or None if there is not one.

        None is the answer today for every run: no stage produces a bundle yet.
        The caller fails the run rather than substituting something else.
        """
        async with self._c.uow() as uow:
            artifacts = await uow.artifacts.list_for_run(run_id)
        for artifact in reversed(artifacts):
            if artifact.kind == DEPLOYABLE_ARTIFACT_KIND:
                return artifact.uri
        return None

    async def _reconcile(self, job: Job) -> None:
        report = await self._c.reconciler.inspect()
        if report.is_clean:
            await self._c.jobs.succeed(job)
            return
        # The worker reports; it does not delete. Orphan cleanup is an operator
        # action -- see RUNBOOK.md -- because a reconciler that acts on its own
        # can delete a run someone is still looking at.
        log.warning(
            "reconcile.discrepancies",
            count=len(report.all),
            subjects=[d.subject for d in report.all][:20],
        )
        await self._c.jobs.succeed(job)

    async def _advance(
        self, runs: RunService, run_id: UUID, trigger: Trigger, *, detail: str
    ) -> None:
        try:
            await runs.advance(run_id, trigger, actor="worker", detail=detail)
        except IllegalTransitionError:
            # The run moved while we worked -- cancelled, or a retry of a job
            # whose transition already landed. Both are expected under retry,
            # and neither should fail the job we just finished.
            log.info("worker.transition_already_applied", run_id=str(run_id), trigger=trigger.value)


async def run_worker(settings: Settings) -> None:
    configure_logging(settings)
    container = build_container(settings)
    worker = Worker(container)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.request_stop)

    try:
        await worker.run_forever()
    finally:
        await container.aclose()


def main() -> None:
    try:
        settings = get_settings()
    except SlipwayError as exc:
        raise SystemExit(str(exc)) from exc
    asyncio.run(run_worker(settings))


if __name__ == "__main__":
    main()
