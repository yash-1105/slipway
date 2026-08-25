"""SQLAlchemy implementations of the domain repository Protocols."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import tables as t
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
    Run,
    RunState,
)
from app.domain.errors import AlreadyDecidedError


class SqlRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(self, run: Run) -> Run:
        await self._s.execute(
            insert(t.runs).values(
                id=run.id,
                brief=run.brief,
                title=run.title,
                state=run.state.value,
                failure_reason=run.failure_reason,
                created_at=run.created_at,
                updated_at=run.updated_at,
            )
        )
        return run

    async def get(self, run_id: UUID) -> Run | None:
        result = await self._s.execute(select(t.runs).where(t.runs.c.id == run_id))
        row = result.mappings().first()
        return _to_run(dict(row)) if row else None

    async def list(
        self, *, states: frozenset[RunState] | None = None, limit: int = 50
    ) -> list[Run]:
        query = select(t.runs).order_by(t.runs.c.id.desc()).limit(limit)
        if states:
            query = query.where(t.runs.c.state.in_([s.value for s in states]))
        rows = (await self._s.execute(query)).mappings().all()
        return [_to_run(dict(r)) for r in rows]

    async def update_state(
        self,
        run_id: UUID,
        *,
        expected: RunState,
        new_state: RunState,
        failure_reason: str | None = None,
    ) -> Run | None:
        result = await self._s.execute(
            update(t.runs)
            .where(and_(t.runs.c.id == run_id, t.runs.c.state == expected.value))
            .values(
                state=new_state.value,
                failure_reason=failure_reason,
                updated_at=datetime.now(UTC),
            )
            .returning(t.runs)
        )
        row = result.mappings().first()
        # None means the compare-and-set lost: someone else moved the run
        # first. The caller re-reads rather than clobbering the winner.
        return _to_run(dict(row)) if row else None


class SqlEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def append(self, event: Event) -> Event:
        await self._s.execute(
            insert(t.events).values(
                id=event.id,
                run_id=event.run_id,
                kind=event.kind,
                payload=event.payload,
                created_at=event.created_at,
            )
        )
        return event

    async def list_for_run(self, run_id: UUID, *, limit: int = 200) -> list[Event]:
        rows = (
            (
                await self._s.execute(
                    select(t.events)
                    .where(t.events.c.run_id == run_id)
                    .order_by(t.events.c.id)
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )
        return [
            Event(
                id=r["id"],
                run_id=r["run_id"],
                kind=r["kind"],
                created_at=r["created_at"],
                payload=dict(r["payload"]),
            )
            for r in rows
        ]


class SqlJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def enqueue(self, job: Job) -> Job:
        now = datetime.now(UTC)
        try:
            async with self._s.begin_nested():
                await self._s.execute(
                    insert(t.jobs).values(
                        id=job.id,
                        run_id=job.run_id,
                        kind=job.kind,
                        status=job.status.value,
                        idempotency_key=job.idempotency_key,
                        attempts=job.attempts,
                        run_after=now,
                        created_at=job.created_at,
                        updated_at=now,
                    )
                )
        except IntegrityError:
            # The idempotency key already exists, which is exactly what it is
            # for: this enqueue is a retry of one that already landed.
            existing = (
                (
                    await self._s.execute(
                        select(t.jobs).where(t.jobs.c.idempotency_key == job.idempotency_key)
                    )
                )
                .mappings()
                .first()
            )
            if existing is None:  # pragma: no cover -- would mean the constraint lied
                raise
            return _to_job(dict(existing))
        return job

    async def claim(
        self, *, worker_id: str, lease_seconds: int, kinds: frozenset[str]
    ) -> Job | None:
        now = datetime.now(UTC)
        # Claimable: pending and due, or leased with an expired lease. The
        # second half is how a dead worker's job comes back.
        claimable = select(t.jobs.c.id).where(
            and_(
                t.jobs.c.kind.in_(tuple(kinds)),
                t.jobs.c.run_after <= now,
                or_(
                    t.jobs.c.status == JobStatus.PENDING.value,
                    and_(
                        t.jobs.c.status == JobStatus.LEASED.value,
                        t.jobs.c.lease_expires_at < now,
                    ),
                ),
            )
        ).order_by(t.jobs.c.created_at).limit(1).with_for_update(skip_locked=True)

        result = await self._s.execute(
            update(t.jobs)
            .where(t.jobs.c.id.in_(claimable.scalar_subquery()))
            .values(
                status=JobStatus.LEASED.value,
                leased_by=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempts=t.jobs.c.attempts + 1,
                updated_at=now,
            )
            .returning(t.jobs)
        )
        row = result.mappings().first()
        return _to_job(dict(row)) if row else None

    async def finish(
        self, job_id: UUID, *, status: JobStatus, error: str | None = None
    ) -> None:
        await self._s.execute(
            update(t.jobs)
            .where(t.jobs.c.id == job_id)
            .values(
                status=status.value,
                leased_by=None,
                lease_expires_at=None,
                last_error=error,
                updated_at=datetime.now(UTC),
            )
        )

    async def extend_lease(self, job_id: UUID, *, worker_id: str, lease_seconds: int) -> bool:
        now = datetime.now(UTC)
        result = await self._s.execute(
            update(t.jobs)
            .where(
                and_(
                    t.jobs.c.id == job_id,
                    t.jobs.c.leased_by == worker_id,
                    t.jobs.c.lease_expires_at > now,
                )
            )
            .values(lease_expires_at=now + timedelta(seconds=lease_seconds), updated_at=now)
            .returning(t.jobs.c.id)
        )
        # False means we already lost the lease; the caller must stop working.
        return result.first() is not None

    async def list_expired(self, *, now: datetime, limit: int = 100) -> list[Job]:
        rows = (
            (
                await self._s.execute(
                    select(t.jobs)
                    .where(
                        and_(
                            t.jobs.c.status == JobStatus.LEASED.value,
                            t.jobs.c.lease_expires_at < now,
                        )
                    )
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )
        return [_to_job(dict(r)) for r in rows]


class SqlApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def record(self, approval: Approval) -> Approval:
        try:
            async with self._s.begin_nested():
                await self._s.execute(
                    insert(t.approvals).values(
                        id=approval.id,
                        run_id=approval.run_id,
                        gate=approval.gate.value,
                        approved=approval.approved,
                        decided_by=approval.decided_by,
                        note=approval.note,
                        decided_at=approval.decided_at,
                    )
                )
        except IntegrityError as exc:
            raise AlreadyDecidedError(approval.run_id, approval.gate.value) from exc
        return approval

    async def get(self, run_id: UUID, gate: Gate) -> Approval | None:
        row = (
            (
                await self._s.execute(
                    select(t.approvals).where(
                        and_(t.approvals.c.run_id == run_id, t.approvals.c.gate == gate.value)
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return Approval(
            id=row["id"],
            run_id=row["run_id"],
            gate=Gate(row["gate"]),
            approved=row["approved"],
            decided_by=row["decided_by"],
            decided_at=row["decided_at"],
            note=row["note"],
        )


class SqlArtifactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def record(self, artifact: ArtifactRef) -> ArtifactRef:
        try:
            async with self._s.begin_nested():
                await self._s.execute(
                    insert(t.artifacts).values(
                        id=artifact.id,
                        run_id=artifact.run_id,
                        kind=artifact.kind,
                        uri=artifact.uri,
                        sha256=artifact.sha256,
                        size_bytes=artifact.size_bytes,
                        created_at=artifact.created_at,
                    )
                )
        except IntegrityError:
            # Same bytes, same kind, same run: the artifact is already recorded
            # and this is a retry. Return what is stored.
            row = (
                (
                    await self._s.execute(
                        select(t.artifacts).where(
                            and_(
                                t.artifacts.c.run_id == artifact.run_id,
                                t.artifacts.c.kind == artifact.kind,
                                t.artifacts.c.sha256 == artifact.sha256,
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:  # pragma: no cover -- would mean the constraint lied
                raise
            return _to_artifact(dict(row))
        return artifact

    async def list_for_run(self, run_id: UUID) -> list[ArtifactRef]:
        rows = (
            (
                await self._s.execute(
                    select(t.artifacts)
                    .where(t.artifacts.c.run_id == run_id)
                    .order_by(t.artifacts.c.id)
                )
            )
            .mappings()
            .all()
        )
        return [_to_artifact(dict(r)) for r in rows]


class SqlDeploymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def claim_port(self, record: DeploymentRecord) -> DeploymentRecord | None:
        try:
            async with self._s.begin_nested():
                await self._s.execute(
                    insert(t.deployments).values(
                        id=record.id,
                        run_id=record.run_id,
                        status=record.status.value,
                        host=record.host,
                        port=record.port,
                        project_name=record.project_name,
                        artifact_uri=record.artifact_uri,
                        container_name=record.container_name,
                        container_id=record.container_id,
                        image_tag=record.image_tag,
                        container_port=record.container_port,
                        network=record.network,
                        url=record.url,
                        log=record.log,
                        created_at=record.created_at,
                    )
                )
        except IntegrityError:
            # The partial unique index says a live deployment already holds
            # this port. Losing is normal: the caller tries the next one.
            return None
        return record

    async def get(self, deployment_id: UUID) -> DeploymentRecord | None:
        result = await self._s.execute(
            select(t.deployments).where(t.deployments.c.id == deployment_id)
        )
        row = result.mappings().first()
        return _to_deployment(dict(row)) if row else None

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
        now = datetime.now(UTC)
        values: dict[str, object] = {"status": status.value, "settled_at": now}
        if url is not None:
            values["url"] = url
        if container_id is not None:
            values["container_id"] = container_id
        if log is not None:
            values["log"] = log
        if release_port:
            # The only thing that gives a port back. Doing it here means
            # releasing the port and recording the outcome are one statement.
            values["destroyed_at"] = now

        result = await self._s.execute(
            update(t.deployments)
            .where(t.deployments.c.id == deployment_id)
            .values(**values)
            .returning(t.deployments)
        )
        row = result.mappings().first()
        return _to_deployment(dict(row)) if row else None

    async def list_holding_ports(self, *, host: str) -> list[DeploymentRecord]:
        rows = (
            (
                await self._s.execute(
                    select(t.deployments).where(
                        and_(
                            t.deployments.c.host == host,
                            t.deployments.c.destroyed_at.is_(None),
                        )
                    )
                )
            )
            .mappings()
            .all()
        )
        return [_to_deployment(dict(r)) for r in rows]

    async def list_for_run(self, run_id: UUID) -> list[DeploymentRecord]:
        rows = (
            (
                await self._s.execute(
                    select(t.deployments)
                    .where(t.deployments.c.run_id == run_id)
                    .order_by(t.deployments.c.id)
                )
            )
            .mappings()
            .all()
        )
        return [_to_deployment(dict(r)) for r in rows]


class SqlCostRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def record(self, entry: CostEntry) -> CostEntry:
        await self._s.execute(insert(t.cost_entries).values(**_cost_values(entry)))
        return entry

    async def record_many(self, entries: list[CostEntry]) -> None:
        if not entries:
            return
        # One statement. These are written inside the transaction that records
        # the job outcome, so the fewer round trips held open there the better.
        await self._s.execute(
            insert(t.cost_entries), [_cost_values(entry) for entry in entries]
        )

    async def list_for_run(self, run_id: UUID) -> list[CostEntry]:
        rows = (
            (
                await self._s.execute(
                    select(t.cost_entries)
                    .where(t.cost_entries.c.run_id == run_id)
                    .order_by(t.cost_entries.c.id)
                )
            )
            .mappings()
            .all()
        )
        return [_to_cost_entry(dict(r)) for r in rows]


def _to_deployment(row: dict[str, object]) -> DeploymentRecord:
    def maybe(key: str) -> str | None:
        value = row.get(key)
        return None if value is None else str(value)

    return DeploymentRecord(
        id=row["id"],  # type: ignore[arg-type]
        run_id=row["run_id"],  # type: ignore[arg-type]
        status=DeploymentStatus(str(row["status"])),
        host=str(row["host"]),
        port=int(row["port"]),  # type: ignore[call-overload]
        project_name=str(row["project_name"]),
        artifact_uri=str(row["artifact_uri"]),
        created_at=row["created_at"],  # type: ignore[arg-type]
        container_name=maybe("container_name"),
        container_id=maybe("container_id"),
        image_tag=maybe("image_tag"),
        container_port=int(row["container_port"]),  # type: ignore[call-overload]
        network=maybe("network"),
        url=maybe("url"),
        log=maybe("log"),
        settled_at=row.get("settled_at"),  # type: ignore[arg-type]
        destroyed_at=row.get("destroyed_at"),  # type: ignore[arg-type]
    )


def _cost_values(entry: CostEntry) -> dict[str, object]:
    return {
        "id": entry.id,
        "run_id": entry.run_id,
        "job_id": entry.job_id,
        "role": entry.role,
        "model_requested": entry.model_requested,
        "model_used": entry.model_used,
        "prompt_tokens": entry.prompt_tokens,
        "completion_tokens": entry.completion_tokens,
        "usd": entry.usd,
        "inr": entry.inr,
        "usd_to_inr": entry.usd_to_inr,
        "priced": entry.priced,
        "actor": entry.actor,
        "created_at": entry.created_at,
    }


def _to_cost_entry(row: dict[str, object]) -> CostEntry:
    return CostEntry(
        id=row["id"],  # type: ignore[arg-type]
        run_id=row["run_id"],  # type: ignore[arg-type]
        job_id=row["job_id"],  # type: ignore[arg-type]
        role=str(row["role"]),
        model_requested=str(row["model_requested"]),
        model_used=str(row["model_used"]),
        prompt_tokens=int(row["prompt_tokens"]),  # type: ignore[call-overload]
        completion_tokens=int(row["completion_tokens"]),  # type: ignore[call-overload]
        usd=Decimal(str(row["usd"])),
        inr=Decimal(str(row["inr"])),
        usd_to_inr=Decimal(str(row["usd_to_inr"])),
        priced=bool(row["priced"]),
        actor=str(row["actor"]),
        created_at=row["created_at"],  # type: ignore[arg-type]
    )


def _to_run(row: dict[str, object]) -> Run:
    return Run(
        id=row["id"],  # type: ignore[arg-type]
        brief=str(row["brief"]),
        state=RunState(str(row["state"])),
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
        title=row["title"] if row["title"] is None else str(row["title"]),
        failure_reason=(
            row["failure_reason"] if row["failure_reason"] is None else str(row["failure_reason"])
        ),
    )


def _to_job(row: dict[str, object]) -> Job:
    return Job(
        id=row["id"],  # type: ignore[arg-type]
        run_id=row["run_id"],  # type: ignore[arg-type]
        kind=str(row["kind"]),
        status=JobStatus(str(row["status"])),
        idempotency_key=str(row["idempotency_key"]),
        attempts=int(row["attempts"]),  # type: ignore[call-overload]
        created_at=row["created_at"],  # type: ignore[arg-type]
        lease_expires_at=row["lease_expires_at"],  # type: ignore[arg-type]
        leased_by=row["leased_by"] if row["leased_by"] is None else str(row["leased_by"]),
        last_error=row["last_error"] if row["last_error"] is None else str(row["last_error"]),
    )


def _to_artifact(row: dict[str, object]) -> ArtifactRef:
    return ArtifactRef(
        id=row["id"],  # type: ignore[arg-type]
        run_id=row["run_id"],  # type: ignore[arg-type]
        kind=str(row["kind"]),
        uri=str(row["uri"]),
        sha256=str(row["sha256"]),
        size_bytes=int(row["size_bytes"]),  # type: ignore[call-overload]
        created_at=row["created_at"],  # type: ignore[arg-type]
    )


__all__ = [
    "SqlApprovalRepository",
    "SqlArtifactRepository",
    "SqlCostRepository",
    "SqlDeploymentRepository",
    "SqlEventRepository",
    "SqlJobRepository",
    "SqlRunRepository",
]
