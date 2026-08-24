"""In-memory implementations of the domain repository Protocols.

These are test *doubles*, not mocks: they implement the same contract and hold
real state, so a test asserts on what came back rather than on what was called.
Services are tested against these; the SQL implementations are tested against a
real Postgres in tests/integration.
"""

from __future__ import annotations

from datetime import datetime
from types import TracebackType
from uuid import UUID

from app.domain.entities import (
    Approval,
    ArtifactRef,
    CostEntry,
    DeploymentRecord,
    DeploymentStatus,
    Event,
    Gate,
    Job,
    JobStatus,
    PortAllocation,
    Run,
    RunState,
)
from app.domain.errors import AlreadyDecidedError
from app.domain.ids import uuid7


class InMemoryStore:
    """The shared state behind one fake database."""

    def __init__(self) -> None:
        self.runs: dict[UUID, Run] = {}
        self.events: list[Event] = []
        self.jobs: dict[UUID, Job] = {}
        self.approvals: dict[tuple[UUID, Gate], Approval] = {}
        self.artifacts: list[ArtifactRef] = []
        self.ports: dict[UUID, PortAllocation] = {}
        self.costs: list[CostEntry] = []
        self.deployments: dict[UUID, DeploymentRecord] = {}


class _Runs:
    def __init__(self, store: InMemoryStore) -> None:
        self._s = store

    async def create(self, run: Run) -> Run:
        self._s.runs[run.id] = run
        return run

    async def get(self, run_id: UUID) -> Run | None:
        return self._s.runs.get(run_id)

    async def list(
        self, *, states: frozenset[RunState] | None = None, limit: int = 50
    ) -> list[Run]:
        found = [r for r in self._s.runs.values() if states is None or r.state in states]
        return sorted(found, key=lambda r: r.id, reverse=True)[:limit]

    async def update_state(
        self,
        run_id: UUID,
        *,
        expected: RunState,
        new_state: RunState,
        failure_reason: str | None = None,
    ) -> Run | None:
        run = self._s.runs.get(run_id)
        if run is None or run.state is not expected:
            return None
        from dataclasses import replace

        updated = replace(
            run,
            state=new_state,
            failure_reason=failure_reason,
            updated_at=datetime.now(run.updated_at.tzinfo),
        )
        self._s.runs[run_id] = updated
        return updated


class _Events:
    def __init__(self, store: InMemoryStore) -> None:
        self._s = store

    async def append(self, event: Event) -> Event:
        self._s.events.append(event)
        return event

    async def list_for_run(self, run_id: UUID, *, limit: int = 200) -> list[Event]:
        return [e for e in self._s.events if e.run_id == run_id][:limit]


class _Jobs:
    def __init__(self, store: InMemoryStore) -> None:
        self._s = store

    async def enqueue(self, job: Job) -> Job:
        for existing in self._s.jobs.values():
            if existing.idempotency_key == job.idempotency_key:
                return existing
        self._s.jobs[job.id] = job
        return job

    async def claim(
        self, *, worker_id: str, lease_seconds: int, kinds: frozenset[str]
    ) -> Job | None:
        from dataclasses import replace

        for job in sorted(self._s.jobs.values(), key=lambda j: j.created_at):
            if job.kind in kinds and job.status is JobStatus.PENDING:
                claimed = replace(job, status=JobStatus.LEASED, leased_by=worker_id,
                                  attempts=job.attempts + 1)
                self._s.jobs[job.id] = claimed
                return claimed
        return None

    async def finish(self, job_id: UUID, *, status: JobStatus, error: str | None = None) -> None:
        from dataclasses import replace

        job = self._s.jobs.get(job_id)
        if job is not None:
            self._s.jobs[job_id] = replace(job, status=status, last_error=error, leased_by=None)

    async def extend_lease(self, job_id: UUID, *, worker_id: str, lease_seconds: int) -> bool:
        job = self._s.jobs.get(job_id)
        return job is not None and job.leased_by == worker_id

    async def list_expired(self, *, now: datetime, limit: int = 100) -> list[Job]:
        return []


class _Approvals:
    def __init__(self, store: InMemoryStore) -> None:
        self._s = store

    async def record(self, approval: Approval) -> Approval:
        key = (approval.run_id, approval.gate)
        if key in self._s.approvals:
            raise AlreadyDecidedError(approval.run_id, approval.gate.value)
        self._s.approvals[key] = approval
        return approval

    async def get(self, run_id: UUID, gate: Gate) -> Approval | None:
        return self._s.approvals.get((run_id, gate))


class _Artifacts:
    def __init__(self, store: InMemoryStore) -> None:
        self._s = store

    async def record(self, artifact: ArtifactRef) -> ArtifactRef:
        for existing in self._s.artifacts:
            if (existing.run_id, existing.kind, existing.sha256) == (
                artifact.run_id,
                artifact.kind,
                artifact.sha256,
            ):
                return existing
        self._s.artifacts.append(artifact)
        return artifact

    async def list_for_run(self, run_id: UUID) -> list[ArtifactRef]:
        return [a for a in self._s.artifacts if a.run_id == run_id]


class _Ports:
    def __init__(self, store: InMemoryStore) -> None:
        self._s = store

    async def allocate(self, *, run_id: UUID, host: str, port: int) -> PortAllocation | None:
        for allocation in self._s.ports.values():
            live = allocation.released_at is None
            if live and (allocation.host, allocation.port) == (host, port):
                return None
        from datetime import UTC

        created = PortAllocation(
            id=uuid7(), run_id=run_id, host=host, port=port, allocated_at=datetime.now(UTC)
        )
        self._s.ports[created.id] = created
        return created

    async def release(self, allocation_id: UUID) -> None:
        from dataclasses import replace
        from datetime import UTC

        allocation = self._s.ports.get(allocation_id)
        if allocation is not None:
            self._s.ports[allocation_id] = replace(allocation, released_at=datetime.now(UTC))

    async def list_active(self, *, host: str) -> list[PortAllocation]:
        return [
            a for a in self._s.ports.values() if a.host == host and a.released_at is None
        ]


class _Costs:
    def __init__(self, store: InMemoryStore) -> None:
        self._s = store

    async def record(self, entry: CostEntry) -> CostEntry:
        self._s.costs.append(entry)
        return entry

    async def record_many(self, entries: list[CostEntry]) -> None:
        self._s.costs.extend(entries)

    async def list_for_run(self, run_id: UUID) -> list[CostEntry]:
        return [c for c in self._s.costs if c.run_id == run_id]


class _Deployments:
    def __init__(self, store: InMemoryStore) -> None:
        self._s = store

    async def claim_port(self, record: DeploymentRecord) -> DeploymentRecord | None:
        for existing in self._s.deployments.values():
            if existing.port == record.port and existing.destroyed_at is None:
                return None
        self._s.deployments[record.id] = record
        return record

    async def get(self, deployment_id: UUID) -> DeploymentRecord | None:
        return self._s.deployments.get(deployment_id)

    async def settle(
        self,
        deployment_id: UUID,
        *,
        status: DeploymentStatus,
        url: str | None = None,
        container_id: str | None = None,
        log: str | None = None,
        release_port: bool = False,
    ) -> DeploymentRecord | None:
        from dataclasses import replace
        from datetime import UTC

        record = self._s.deployments.get(deployment_id)
        if record is None:
            return None
        now = datetime.now(UTC)
        updated = replace(
            record,
            status=status,
            url=url if url is not None else record.url,
            container_id=container_id if container_id is not None else record.container_id,
            log=log if log is not None else record.log,
            settled_at=now,
            destroyed_at=now if release_port else record.destroyed_at,
        )
        self._s.deployments[deployment_id] = updated
        return updated

    async def list_holding_ports(self, *, host: str) -> list[DeploymentRecord]:
        return [
            d for d in self._s.deployments.values()
            if d.host == host and d.destroyed_at is None
        ]

    async def list_for_run(self, run_id: UUID) -> list[DeploymentRecord]:
        return [d for d in self._s.deployments.values() if d.run_id == run_id]


class InMemoryUnitOfWork:
    """Implements app.domain.repositories.UnitOfWork over an InMemoryStore.

    Commit is a no-op and rollback does not undo: these fakes exist to test the
    logic above the repository boundary, not transaction semantics. Transaction
    behaviour is tested against a real Postgres in tests/integration.
    """

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store
        self.runs = _Runs(store)
        self.events = _Events(store)
        self.jobs = _Jobs(store)
        self.approvals = _Approvals(store)
        self.artifacts = _Artifacts(store)
        self.ports = _Ports(store)
        self.costs = _Costs(store)
        self.deployments = _Deployments(store)
        self.commits = 0

    async def __aenter__(self) -> InMemoryUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None
