"""RunService against in-memory repositories.

The service is the thing under test; the repositories are doubles at the
repository boundary. Assertions are on the state that came back, never on
whether a method was called.
"""

from __future__ import annotations

import pytest

from app.domain.entities import Gate, JobStatus, RunState, Trigger
from app.domain.errors import AlreadyDecidedError, IllegalTransitionError, NotFoundError
from app.domain.ids import uuid7
from app.services.runs import RunService
from tests.unit.fakes import InMemoryStore, InMemoryUnitOfWork


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def runs(store: InMemoryStore) -> RunService:
    return RunService(lambda: InMemoryUnitOfWork(store))


async def test_creating_a_run_starts_it_and_enqueues_the_first_job(
    runs: RunService, store: InMemoryStore
) -> None:
    run = await runs.create("build me a booking form", title="acme")

    assert run.state is RunState.SPECIFYING
    assert [job.kind for job in store.jobs.values()] == ["specify"]
    assert [event.kind for event in store.events] == ["run.created", "run.transitioned"]


async def test_a_run_waits_at_the_spec_gate(runs: RunService) -> None:
    run = await runs.create("brief")
    run = await runs.advance(run.id, Trigger.AGENT_SUCCEEDED, actor="worker")

    assert run.state is RunState.SPEC_REVIEW
    assert not run.is_terminal


async def test_approving_the_spec_gate_enqueues_the_build(
    runs: RunService, store: InMemoryStore
) -> None:
    run = await runs.create("brief")
    await runs.advance(run.id, Trigger.AGENT_SUCCEEDED, actor="worker")
    run = await runs.decide(run.id, Gate.SPEC, approved=True, decided_by="yash")

    assert run.state is RunState.BUILDING
    assert sorted(job.kind for job in store.jobs.values()) == ["build", "specify"]
    assert store.approvals[(run.id, Gate.SPEC)].approved is True


async def test_rejecting_the_spec_gate_sends_it_back_to_specifying(runs: RunService) -> None:
    run = await runs.create("brief")
    await runs.advance(run.id, Trigger.AGENT_SUCCEEDED, actor="worker")
    run = await runs.decide(
        run.id, Gate.SPEC, approved=False, decided_by="yash", note="missing the auth flow"
    )

    assert run.state is RunState.SPECIFYING


async def test_a_gate_cannot_be_decided_twice(runs: RunService) -> None:
    """The (run, gate) unique constraint is what makes a double-click safe."""
    run = await runs.create("brief")
    await runs.advance(run.id, Trigger.AGENT_SUCCEEDED, actor="worker")
    await runs.decide(run.id, Gate.SPEC, approved=True, decided_by="yash")

    with pytest.raises((AlreadyDecidedError, IllegalTransitionError)):
        await runs.decide(run.id, Gate.SPEC, approved=True, decided_by="yash")


async def test_a_gate_that_is_not_open_cannot_be_decided(runs: RunService) -> None:
    run = await runs.create("brief")  # SPECIFYING, no gate open

    with pytest.raises(IllegalTransitionError):
        await runs.decide(run.id, Gate.SPEC, approved=True, decided_by="yash")


async def test_the_wrong_gate_cannot_be_decided(runs: RunService) -> None:
    run = await runs.create("brief")
    await runs.advance(run.id, Trigger.AGENT_SUCCEEDED, actor="worker")  # SPEC_REVIEW

    with pytest.raises(IllegalTransitionError):
        await runs.decide(run.id, Gate.DEPLOY, approved=True, decided_by="yash")


async def test_a_looping_run_enqueues_real_work_each_time(
    runs: RunService, store: InMemoryStore
) -> None:
    """Reject, respecify, approve: the second build must not collide with the first.

    Job idempotency keys are derived from the transition that caused them, so a
    run that goes round the loop twice gets two jobs rather than silently one.
    """
    run = await runs.create("brief")
    await runs.advance(run.id, Trigger.AGENT_SUCCEEDED, actor="worker")
    await runs.decide(run.id, Gate.SPEC, approved=True, decided_by="yash")  # BUILDING
    await runs.advance(run.id, Trigger.AGENT_SUCCEEDED, actor="worker")  # TESTING
    await runs.advance(run.id, Trigger.AGENT_SUCCEEDED, actor="worker")  # DEPLOY_REVIEW
    await runs.decide(run.id, Gate.DEPLOY, approved=False, decided_by="yash")  # BUILDING again

    build_jobs = [job for job in store.jobs.values() if job.kind == "build"]
    assert len(build_jobs) == 2
    assert len({job.idempotency_key for job in build_jobs}) == 2


async def test_re_enqueueing_the_same_transition_is_idempotent(
    runs: RunService, store: InMemoryStore
) -> None:
    """The same logical work enqueued twice is one job, not two."""
    from datetime import UTC, datetime

    from app.domain.entities import Job

    run = await runs.create("brief")
    existing = next(iter(store.jobs.values()))

    async with InMemoryUnitOfWork(store) as uow:
        returned = await uow.jobs.enqueue(
            Job(
                id=uuid7(),
                run_id=run.id,
                kind="specify",
                status=JobStatus.PENDING,
                idempotency_key=existing.idempotency_key,
                attempts=0,
                created_at=datetime.now(UTC),
            )
        )

    assert returned.id == existing.id
    assert len(store.jobs) == 1


async def test_cancelling_works_from_any_live_state(runs: RunService) -> None:
    run = await runs.create("brief")
    run = await runs.cancel(run.id, actor="yash", reason="client pulled the project")

    assert run.state is RunState.CANCELLED
    assert run.is_terminal


async def test_a_cancelled_run_cannot_be_advanced(runs: RunService) -> None:
    run = await runs.create("brief")
    await runs.cancel(run.id, actor="yash", reason="stop")

    with pytest.raises(IllegalTransitionError):
        await runs.advance(run.id, Trigger.AGENT_SUCCEEDED, actor="worker")


async def test_a_failing_agent_records_why(runs: RunService) -> None:
    run = await runs.create("brief")
    run = await runs.advance(
        run.id, Trigger.AGENT_FAILED, actor="worker", detail="timeout: provider did not respond"
    )

    assert run.state is RunState.FAILED
    assert run.failure_reason == "timeout: provider did not respond"


async def test_a_failed_run_always_has_a_reason(runs: RunService) -> None:
    """The database CHECK requires it; the service must not be able to violate it."""
    run = await runs.create("brief")
    run = await runs.advance(run.id, Trigger.AGENT_FAILED, actor="worker")

    assert run.state is RunState.FAILED
    assert run.failure_reason


async def test_an_unknown_run_is_reported_as_not_found(runs: RunService) -> None:
    with pytest.raises(NotFoundError):
        await runs.get(uuid7())


async def test_the_event_log_records_every_transition(
    runs: RunService, store: InMemoryStore
) -> None:
    """The gates are judged against this log, so it must be complete."""
    run = await runs.create("brief")
    await runs.advance(run.id, Trigger.AGENT_SUCCEEDED, actor="worker")
    await runs.decide(run.id, Gate.SPEC, approved=True, decided_by="yash", note="looks right")

    transitions = [e for e in store.events if e.kind == "run.transitioned"]
    assert [(e.payload["from"], e.payload["to"]) for e in transitions] == [
        ("created", "specifying"),
        ("specifying", "spec_review"),
        ("spec_review", "building"),
    ]
    assert transitions[-1].payload["actor"] == "yash"
    assert transitions[-1].payload["detail"] == "looks right"
