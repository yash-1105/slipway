"""Claiming, heartbeating and finishing worker jobs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import structlog

from app.domain.entities import Event, Job, JobStatus
from app.domain.ids import uuid7
from app.domain.repositories import UnitOfWork

log = structlog.get_logger(__name__)

UowFactory = Callable[[], UnitOfWork]


class JobService:
    def __init__(
        self,
        uow_factory: UowFactory,
        *,
        worker_id: str,
        lease_seconds: int,
        max_attempts: int,
    ) -> None:
        self._uow = uow_factory
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts

    async def claim(self, kinds: frozenset[str]) -> Job | None:
        async with self._uow() as uow:
            job = await uow.jobs.claim(
                worker_id=self._worker_id, lease_seconds=self._lease_seconds, kinds=kinds
            )
            await uow.commit()
        if job is not None:
            log.info(
                "job.claimed",
                job_id=str(job.id),
                run_id=str(job.run_id),
                kind=job.kind,
                attempt=job.attempts,
            )
        return job

    async def heartbeat(self, job_id: UUID) -> bool:
        """Extend the lease. False means we lost it and must stop working."""
        async with self._uow() as uow:
            held = await uow.jobs.extend_lease(
                job_id, worker_id=self._worker_id, lease_seconds=self._lease_seconds
            )
            await uow.commit()
        if not held:
            log.warning("job.lease_lost", job_id=str(job_id), worker=self._worker_id)
        return held

    async def succeed(self, job: Job) -> None:
        async with self._uow() as uow:
            await uow.jobs.finish(job.id, status=JobStatus.SUCCEEDED)
            await uow.events.append(
                Event(
                    id=uuid7(),
                    run_id=job.run_id,
                    kind="job.succeeded",
                    created_at=datetime.now(UTC),
                    payload={"job_id": str(job.id), "kind": job.kind, "attempts": job.attempts},
                )
            )
            await uow.commit()

    async def fail(self, job: Job, error: str) -> bool:
        """Record a failed attempt.

        Returns True if the job will be retried, False if it is exhausted. The
        caller uses that to decide whether to fail the run, so an exhausted job
        does not leave a run waiting forever.
        """
        exhausted = job.attempts >= self._max_attempts
        status = JobStatus.ABANDONED if exhausted else JobStatus.PENDING

        async with self._uow() as uow:
            await uow.jobs.finish(job.id, status=status, error=error)
            await uow.events.append(
                Event(
                    id=uuid7(),
                    run_id=job.run_id,
                    kind="job.abandoned" if exhausted else "job.failed",
                    created_at=datetime.now(UTC),
                    payload={
                        "job_id": str(job.id),
                        "kind": job.kind,
                        "attempts": job.attempts,
                        "error": error[:2000],
                    },
                )
            )
            await uow.commit()

        log.warning(
            "job.abandoned" if exhausted else "job.failed",
            job_id=str(job.id),
            kind=job.kind,
            attempts=job.attempts,
            error=error[:500],
        )
        return not exhausted
