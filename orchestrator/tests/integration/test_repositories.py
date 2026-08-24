"""The SQL repositories against a real Postgres.

The behaviour being tested here is the behaviour the in-memory doubles cannot
have: SKIP LOCKED claiming, lease expiry, unique-constraint arbitration and
compare-and-set.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.entities import Event, Job, JobStatus, Run, RunState
from app.domain.errors import AlreadyDecidedError
from app.domain.ids import uuid7
from tests.integration.conftest import UowFactory

pytestmark = pytest.mark.integration


async def _a_run(uow_factory: UowFactory, state: RunState = RunState.CREATED) -> Run:
    now = datetime.now(UTC)
    run = Run(id=uuid7(), brief="a brief", state=state, created_at=now, updated_at=now)
    async with uow_factory() as uow:
        await uow.runs.create(run)
        await uow.commit()
    return run


def _job(run: Run, *, kind: str = "specify", key: str | None = None) -> Job:
    return Job(
        id=uuid7(),
        run_id=run.id,
        kind=kind,
        status=JobStatus.PENDING,
        idempotency_key=key or f"{run.id}:{kind}:{uuid7()}",
        attempts=0,
        created_at=datetime.now(UTC),
    )


async def test_a_run_round_trips(uow_factory: UowFactory) -> None:
    run = await _a_run(uow_factory)
    async with uow_factory() as uow:
        found = await uow.runs.get(run.id)

    assert found is not None
    assert found.id == run.id
    assert found.state is RunState.CREATED


async def test_compare_and_set_rejects_a_stale_expectation(uow_factory: UowFactory) -> None:
    """A losing racer must find out it lost, not clobber the winner."""
    run = await _a_run(uow_factory)

    async with uow_factory() as uow:
        won = await uow.runs.update_state(
            run.id, expected=RunState.CREATED, new_state=RunState.SPECIFYING
        )
        await uow.commit()
    assert won is not None

    async with uow_factory() as uow:
        lost = await uow.runs.update_state(
            run.id, expected=RunState.CREATED, new_state=RunState.CANCELLED
        )
    assert lost is None


async def test_enqueueing_the_same_idempotency_key_twice_yields_one_job(
    uow_factory: UowFactory,
) -> None:
    run = await _a_run(uow_factory)
    first = _job(run, key="the-same-key")
    second = _job(run, key="the-same-key")

    async with uow_factory() as uow:
        stored = await uow.jobs.enqueue(first)
        await uow.commit()
    async with uow_factory() as uow:
        again = await uow.jobs.enqueue(second)
        await uow.commit()

    assert stored.id == first.id
    assert again.id == first.id, "the second enqueue must return the first job"


async def test_claiming_is_exclusive_under_concurrency(uow_factory: UowFactory) -> None:
    """FOR UPDATE SKIP LOCKED: one job, many workers, exactly one winner."""
    run = await _a_run(uow_factory)
    async with uow_factory() as uow:
        await uow.jobs.enqueue(_job(run))
        await uow.commit()

    async def claim(worker: str) -> Job | None:
        async with uow_factory() as uow:
            claimed = await uow.jobs.claim(
                worker_id=worker, lease_seconds=60, kinds=frozenset({"specify"})
            )
            await uow.commit()
            return claimed

    results = await asyncio.gather(*(claim(f"worker-{i}") for i in range(8)))
    winners = [job for job in results if job is not None]

    assert len(winners) == 1, f"{len(winners)} workers claimed the same job"
    assert winners[0].attempts == 1


async def test_many_jobs_are_spread_across_workers_not_serialised(uow_factory: UowFactory) -> None:
    """SKIP LOCKED means a busy row is skipped, not waited on."""
    run = await _a_run(uow_factory)
    async with uow_factory() as uow:
        for _ in range(5):
            await uow.jobs.enqueue(_job(run))
        await uow.commit()

    async def claim(worker: str) -> Job | None:
        async with uow_factory() as uow:
            claimed = await uow.jobs.claim(
                worker_id=worker, lease_seconds=60, kinds=frozenset({"specify"})
            )
            await uow.commit()
            return claimed

    results = await asyncio.gather(*(claim(f"worker-{i}") for i in range(5)))
    claimed = [job for job in results if job is not None]

    assert len(claimed) == 5
    assert len({job.id for job in claimed}) == 5, "no job claimed twice"


async def test_an_expired_lease_is_reclaimed(uow_factory: UowFactory) -> None:
    """This is how a dead worker's job comes back (RUNBOOK.md)."""
    run = await _a_run(uow_factory)
    async with uow_factory() as uow:
        await uow.jobs.enqueue(_job(run))
        await uow.commit()

    async with uow_factory() as uow:
        # A lease that expired one second ago.
        first = await uow.jobs.claim(
            worker_id="dead-worker", lease_seconds=-1, kinds=frozenset({"specify"})
        )
        await uow.commit()
    assert first is not None

    async with uow_factory() as uow:
        second = await uow.jobs.claim(
            worker_id="live-worker", lease_seconds=60, kinds=frozenset({"specify"})
        )
        await uow.commit()

    assert second is not None
    assert second.id == first.id
    assert second.leased_by == "live-worker"
    assert second.attempts == 2, "the reclaim counts as another attempt"


async def test_a_live_lease_is_not_stolen(uow_factory: UowFactory) -> None:
    run = await _a_run(uow_factory)
    async with uow_factory() as uow:
        await uow.jobs.enqueue(_job(run))
        await uow.commit()

    async with uow_factory() as uow:
        await uow.jobs.claim(worker_id="busy", lease_seconds=300, kinds=frozenset({"specify"}))
        await uow.commit()

    async with uow_factory() as uow:
        stolen = await uow.jobs.claim(
            worker_id="thief", lease_seconds=300, kinds=frozenset({"specify"})
        )

    assert stolen is None


async def test_extending_a_lease_you_no_longer_hold_fails(uow_factory: UowFactory) -> None:
    """A worker that lost its lease must stop working, not carry on."""
    run = await _a_run(uow_factory)
    async with uow_factory() as uow:
        await uow.jobs.enqueue(_job(run))
        await uow.commit()

    async with uow_factory() as uow:
        job = await uow.jobs.claim(
            worker_id="worker-a", lease_seconds=300, kinds=frozenset({"specify"})
        )
        await uow.commit()
    assert job is not None

    async with uow_factory() as uow:
        assert await uow.jobs.extend_lease(job.id, worker_id="worker-a", lease_seconds=300)
        assert not await uow.jobs.extend_lease(job.id, worker_id="worker-b", lease_seconds=300)
        await uow.commit()


async def test_only_requested_job_kinds_are_claimed(uow_factory: UowFactory) -> None:
    run = await _a_run(uow_factory)
    async with uow_factory() as uow:
        await uow.jobs.enqueue(_job(run, kind="deploy"))
        await uow.commit()

    async with uow_factory() as uow:
        claimed = await uow.jobs.claim(
            worker_id="w", lease_seconds=60, kinds=frozenset({"specify"})
        )

    assert claimed is None


async def test_expired_leases_are_listable_for_the_reconciler(uow_factory: UowFactory) -> None:
    run = await _a_run(uow_factory)
    async with uow_factory() as uow:
        await uow.jobs.enqueue(_job(run))
        await uow.commit()
    async with uow_factory() as uow:
        await uow.jobs.claim(worker_id="dead", lease_seconds=-5, kinds=frozenset({"specify"}))
        await uow.commit()

    async with uow_factory() as uow:
        expired = await uow.jobs.list_expired(now=datetime.now(UTC))

    assert len(expired) == 1


async def test_a_gate_can_only_be_decided_once(uow_factory: UowFactory) -> None:
    from app.domain.entities import Approval, Gate

    run = await _a_run(uow_factory)

    def decision(approved: bool) -> Approval:
        return Approval(
            id=uuid7(),
            run_id=run.id,
            gate=Gate.SPEC,
            approved=approved,
            decided_by="yash",
            decided_at=datetime.now(UTC),
        )

    async with uow_factory() as uow:
        await uow.approvals.record(decision(True))
        await uow.commit()

    with pytest.raises(AlreadyDecidedError):
        async with uow_factory() as uow:
            await uow.approvals.record(decision(False))
            await uow.commit()


async def test_a_port_is_held_by_exactly_one_run(uow_factory: UowFactory) -> None:
    """The constraint arbitrates, not a scan for what looks free."""
    first_run = await _a_run(uow_factory)
    second_run = await _a_run(uow_factory)

    async with uow_factory() as uow:
        won = await uow.ports.allocate(run_id=first_run.id, host="h", port=41000)
        await uow.commit()
    assert won is not None

    async with uow_factory() as uow:
        lost = await uow.ports.allocate(run_id=second_run.id, host="h", port=41000)
        await uow.commit()

    assert lost is None, "the second run must lose to the unique index"


async def test_a_released_port_can_be_allocated_again(uow_factory: UowFactory) -> None:
    run = await _a_run(uow_factory)

    async with uow_factory() as uow:
        first = await uow.ports.allocate(run_id=run.id, host="h", port=41001)
        assert first is not None
        await uow.ports.release(first.id)
        await uow.commit()

    async with uow_factory() as uow:
        second = await uow.ports.allocate(run_id=run.id, host="h", port=41001)
        await uow.commit()

    assert second is not None
    assert second.id != first.id


async def test_recording_the_same_artifact_twice_is_one_row(uow_factory: UowFactory) -> None:
    from app.domain.entities import ArtifactRef

    run = await _a_run(uow_factory)

    def artifact() -> ArtifactRef:
        return ArtifactRef(
            id=uuid7(),
            run_id=run.id,
            kind="spec",
            uri="file:///tmp/x",
            sha256="a" * 64,
            size_bytes=10,
            created_at=datetime.now(UTC),
        )

    async with uow_factory() as uow:
        first = await uow.artifacts.record(artifact())
        await uow.commit()
    async with uow_factory() as uow:
        second = await uow.artifacts.record(artifact())
        await uow.commit()

    assert second.id == first.id

    async with uow_factory() as uow:
        assert len(await uow.artifacts.list_for_run(run.id)) == 1


async def test_events_come_back_in_the_order_they_were_written(uow_factory: UowFactory) -> None:
    """UUIDv7 ordering is what makes `ORDER BY id` the creation order."""
    run = await _a_run(uow_factory)

    async with uow_factory() as uow:
        for index in range(20):
            await uow.events.append(
                Event(
                    id=uuid7(),
                    run_id=run.id,
                    kind=f"kind.{index}",
                    created_at=datetime.now(UTC),
                    payload={"index": index},
                )
            )
        await uow.commit()

    async with uow_factory() as uow:
        events = await uow.events.list_for_run(run.id)

    assert [event.payload["index"] for event in events] == list(range(20))


async def test_a_rolled_back_unit_of_work_leaves_nothing(
    uow_factory: UowFactory, engine: AsyncEngine
) -> None:
    """A job that crashes halfway must leave no partial state; it will be retried."""
    now = datetime.now(UTC)
    run = Run(id=uuid7(), brief="never committed", state=RunState.CREATED,
              created_at=now, updated_at=now)

    async with uow_factory() as uow:
        await uow.runs.create(run)
        # No commit: leaving the block rolls back.

    async with engine.connect() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM runs WHERE brief = 'never committed'")
            )
        ).scalar_one()

    assert count == 0


async def test_the_lease_deadline_is_in_the_future_when_claimed(uow_factory: UowFactory) -> None:
    run = await _a_run(uow_factory)
    async with uow_factory() as uow:
        await uow.jobs.enqueue(_job(run))
        await uow.commit()

    async with uow_factory() as uow:
        job = await uow.jobs.claim(
            worker_id="w", lease_seconds=120, kinds=frozenset({"specify"})
        )
        await uow.commit()

    assert job is not None
    assert job.lease_expires_at is not None
    assert job.lease_expires_at > datetime.now(UTC) + timedelta(seconds=60)
