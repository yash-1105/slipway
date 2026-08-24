"""Repository Protocols.

Declared here, in the layer that has no dependencies, and implemented in
app/db/. Services depend on these Protocols, so a database session never
appears in a service signature. See docs/decisions/0002-layering.md.
"""

from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Protocol
from uuid import UUID

from app.domain.entities import (
    Approval,
    ArtifactRef,
    CostEntry,
    Event,
    Gate,
    Job,
    JobStatus,
    PortAllocation,
    Run,
    RunState,
)


class RunRepository(Protocol):
    async def create(self, run: Run) -> Run: ...

    async def get(self, run_id: UUID) -> Run | None: ...

    async def list(
        self, *, states: frozenset[RunState] | None = None, limit: int = 50
    ) -> list[Run]: ...

    async def update_state(
        self,
        run_id: UUID,
        *,
        expected: RunState,
        new_state: RunState,
        failure_reason: str | None = None,
    ) -> Run | None:
        """Compare-and-set the state.

        Returns None if `expected` no longer matches, which is how a losing
        racer finds out it lost rather than clobbering the winner.
        """


class EventRepository(Protocol):
    async def append(self, event: Event) -> Event: ...

    async def list_for_run(self, run_id: UUID, *, limit: int = 200) -> list[Event]: ...


class JobRepository(Protocol):
    async def enqueue(self, job: Job) -> Job:
        """Insert a job, or return the existing one with the same idempotency key."""

    async def claim(
        self, *, worker_id: str, lease_seconds: int, kinds: frozenset[str]
    ) -> Job | None:
        """Claim one job with FOR UPDATE SKIP LOCKED, under a lease."""

    async def finish(
        self, job_id: UUID, *, status: JobStatus, error: str | None = None
    ) -> None: ...

    async def extend_lease(self, job_id: UUID, *, worker_id: str, lease_seconds: int) -> bool:
        """Return False if the lease was already lost to another worker."""

    async def list_expired(self, *, now: datetime, limit: int = 100) -> list[Job]: ...


class ApprovalRepository(Protocol):
    async def record(self, approval: Approval) -> Approval:
        """Insert a decision.

        Raises AlreadyDecidedError on the (run, gate) unique constraint.
        """

    async def get(self, run_id: UUID, gate: Gate) -> Approval | None: ...


class ArtifactRepository(Protocol):
    async def record(self, artifact: ArtifactRef) -> ArtifactRef: ...

    async def list_for_run(self, run_id: UUID) -> list[ArtifactRef]: ...


class PortAllocationRepository(Protocol):
    async def allocate(self, *, run_id: UUID, host: str, port: int) -> PortAllocation | None:
        """Claim one port. Returns None if the unique constraint says it is taken."""

    async def release(self, allocation_id: UUID) -> None: ...

    async def list_active(self, *, host: str) -> list[PortAllocation]: ...


class CostRepository(Protocol):
    async def record(self, entry: CostEntry) -> CostEntry: ...

    async def record_many(self, entries: list[CostEntry]) -> None:
        """Write several entries. Used by the worker to commit a job's cost
        alongside the job's outcome, in one transaction."""

    async def list_for_run(self, run_id: UUID) -> list[CostEntry]: ...


class UnitOfWork(Protocol):
    """One transaction spanning several repositories.

    The repositories are read-only properties rather than attributes so that an
    implementation may expose a narrower concrete type; a mutable attribute
    would be invariant and no implementation could satisfy it.
    """

    @property
    def runs(self) -> RunRepository: ...

    @property
    def events(self) -> EventRepository: ...

    @property
    def jobs(self) -> JobRepository: ...

    @property
    def approvals(self) -> ApprovalRepository: ...

    @property
    def artifacts(self) -> ArtifactRepository: ...

    @property
    def ports(self) -> PortAllocationRepository: ...

    @property
    def costs(self) -> CostRepository: ...

    async def __aenter__(self) -> UnitOfWork: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...
