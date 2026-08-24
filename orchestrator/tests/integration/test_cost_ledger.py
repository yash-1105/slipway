"""The cost ledger, against a real Postgres.

The property that matters is transactional: cost and outcome are written in one
transaction, so a run cannot have an outcome with no cost or a cost with no
outcome. That cannot be tested against an in-memory double whose commit is a
no-op, which is why it lives here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.entities import CostEntry, Job, JobStatus, Run, RunState
from app.domain.ids import uuid7
from app.services.jobs import JobService
from tests.integration.conftest import UowFactory

pytestmark = pytest.mark.integration


async def _a_run(uow_factory: UowFactory) -> Run:
    now = datetime.now(UTC)
    run = Run(id=uuid7(), brief="a brief", state=RunState.CREATED, created_at=now, updated_at=now)
    async with uow_factory() as uow:
        await uow.runs.create(run)
        await uow.commit()
    return run


async def _a_job(uow_factory: UowFactory, run: Run) -> Job:
    job = Job(
        id=uuid7(),
        run_id=run.id,
        kind="specify",
        status=JobStatus.PENDING,
        idempotency_key=str(uuid7()),
        attempts=1,
        created_at=datetime.now(UTC),
    )
    async with uow_factory() as uow:
        await uow.jobs.enqueue(job)
        await uow.commit()
    return job


def _entry(run_id: UUID, job_id: UUID | None = None, **overrides: object) -> CostEntry:
    defaults: dict[str, object] = {
        "id": uuid7(),
        "run_id": run_id,
        "job_id": job_id,
        "role": "planner",
        "model_requested": "vendor-a/primary-model",
        "model_used": "vendor-a/primary-model",
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "usd": Decimal("0.00700000"),
        "inr": Decimal("0.560000"),
        "usd_to_inr": Decimal("80.000000"),
        "priced": True,
        "actor": "worker:test",
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return CostEntry(**defaults)  # type: ignore[arg-type]


async def test_an_entry_round_trips_with_its_decimals_intact(uow_factory: UowFactory) -> None:
    run = await _a_run(uow_factory)

    async with uow_factory() as uow:
        await uow.costs.record(_entry(run.id))
        await uow.commit()

    async with uow_factory() as uow:
        (stored,) = await uow.costs.list_for_run(run.id)

    assert stored.usd == Decimal("0.00700000")
    assert stored.inr == Decimal("0.560000")
    assert stored.usd_to_inr == Decimal("80.000000")
    assert isinstance(stored.usd, Decimal), "money must not come back as a float"
    assert stored.total_tokens == 1500


async def test_the_model_actually_used_is_recorded_separately(uow_factory: UowFactory) -> None:
    run = await _a_run(uow_factory)

    async with uow_factory() as uow:
        await uow.costs.record(
            _entry(run.id, model_requested="vendor-a/asked-for-1", model_used="vendor-b/served-2")
        )
        await uow.commit()

    async with uow_factory() as uow:
        (stored,) = await uow.costs.list_for_run(run.id)

    assert stored.model_requested == "vendor-a/asked-for-1"
    assert stored.model_used == "vendor-b/served-2"


async def test_cost_and_outcome_commit_together(
    uow_factory: UowFactory, engine: AsyncEngine
) -> None:
    """The property the whole design exists for."""
    run = await _a_run(uow_factory)
    job = await _a_job(uow_factory, run)

    jobs = JobService(uow_factory, worker_id="w", lease_seconds=60, max_attempts=3)
    await jobs.succeed(job, costs=[_entry(run.id, job.id), _entry(run.id, job.id)])

    async with engine.connect() as conn:
        status = (
            await conn.execute(text("SELECT status FROM jobs WHERE id = :id"), {"id": job.id})
        ).scalar_one()
        rows = (
            await conn.execute(
                text("SELECT count(*) FROM cost_entries WHERE job_id = :id"), {"id": job.id}
            )
        ).scalar_one()

    assert status == "succeeded"
    assert rows == 2


async def test_a_failed_job_still_records_what_it_spent(uow_factory: UowFactory) -> None:
    """Recording cost only on success makes the expensive failures invisible."""
    run = await _a_run(uow_factory)
    job = await _a_job(uow_factory, run)

    jobs = JobService(uow_factory, worker_id="w", lease_seconds=60, max_attempts=3)
    await jobs.fail(job, "the graph failed", costs=[_entry(run.id, job.id)])

    async with uow_factory() as uow:
        entries = await uow.costs.list_for_run(run.id)

    assert len(entries) == 1


async def test_nothing_is_written_when_the_transaction_rolls_back(
    uow_factory: UowFactory, engine: AsyncEngine
) -> None:
    run = await _a_run(uow_factory)

    async with uow_factory() as uow:
        await uow.costs.record(_entry(run.id))
        # No commit: leaving the block rolls back.

    async with engine.connect() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM cost_entries WHERE run_id = :id"), {"id": run.id}
            )
        ).scalar_one()

    assert count == 0


async def test_recording_many_at_once_is_one_statement(uow_factory: UowFactory) -> None:
    run = await _a_run(uow_factory)

    async with uow_factory() as uow:
        await uow.costs.record_many([_entry(run.id) for _ in range(5)])
        await uow.commit()

    async with uow_factory() as uow:
        assert len(await uow.costs.list_for_run(run.id)) == 5


async def test_recording_no_entries_is_a_no_op(uow_factory: UowFactory) -> None:
    run = await _a_run(uow_factory)

    async with uow_factory() as uow:
        await uow.costs.record_many([])
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.costs.list_for_run(run.id) == []


async def test_the_database_refuses_a_negative_amount(
    uow_factory: UowFactory, engine: AsyncEngine
) -> None:
    """Constraints live in the database, not only in Python."""
    run = await _a_run(uow_factory)

    with pytest.raises(IntegrityError):
        async with uow_factory() as uow:
            await uow.costs.record(_entry(run.id, usd=Decimal("-1")))
            await uow.commit()


async def test_the_database_refuses_a_non_positive_rate(uow_factory: UowFactory) -> None:
    """A row converted at a zero rate is a row whose INR means nothing."""
    run = await _a_run(uow_factory)

    with pytest.raises(IntegrityError):
        async with uow_factory() as uow:
            await uow.costs.record(_entry(run.id, usd_to_inr=Decimal("0")))
            await uow.commit()


async def test_the_database_refuses_a_blank_actor(uow_factory: UowFactory) -> None:
    run = await _a_run(uow_factory)

    with pytest.raises(IntegrityError):
        async with uow_factory() as uow:
            await uow.costs.record(_entry(run.id, actor="   "))
            await uow.commit()


async def test_entries_come_back_in_the_order_they_were_written(
    uow_factory: UowFactory,
) -> None:
    run = await _a_run(uow_factory)
    roles = ["planner", "builder", "evaluator", "test_author", "doc_writer"]

    async with uow_factory() as uow:
        for role in roles:
            await uow.costs.record(_entry(run.id, role=role))
        await uow.commit()

    async with uow_factory() as uow:
        entries = await uow.costs.list_for_run(run.id)

    assert [e.role for e in entries] == roles
