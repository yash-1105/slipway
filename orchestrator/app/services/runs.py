"""Run lifecycle: create, advance through the state machine, decide the gates.

The only place a run's state changes. Every change writes an event in the same
transaction, so the audit trail cannot disagree with the run.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import structlog

from app.domain.entities import Event, Gate, Job, JobStatus, Run, RunState, Trigger
from app.domain.errors import IllegalTransitionError, NotFoundError
from app.domain.ids import uuid7
from app.domain.repositories import UnitOfWork
from app.state import machine

log = structlog.get_logger(__name__)

UowFactory = Callable[[], UnitOfWork]

#: Which worker job kind drives a state forward. States absent from this map
#: are either terminal or waiting on a human, and have no job.
JOB_FOR_STATE: dict[RunState, str] = {
    RunState.SPECIFYING: "specify",
    RunState.BUILDING: "build",
    RunState.TESTING: "test",
    RunState.DEPLOYING: "deploy",
}


class RunService:
    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow = uow_factory

    async def create(self, brief: str, *, title: str | None = None) -> Run:
        now = datetime.now(UTC)
        run = Run(
            id=uuid7(),
            brief=brief,
            title=title,
            state=RunState.CREATED,
            created_at=now,
            updated_at=now,
        )
        async with self._uow() as uow:
            await uow.runs.create(run)
            await uow.events.append(
                Event(
                    id=uuid7(),
                    run_id=run.id,
                    kind="run.created",
                    created_at=now,
                    payload={"title": title or "", "brief_chars": len(brief)},
                )
            )
            await uow.commit()

        log.info("run.created", run_id=str(run.id))
        return await self.advance(run.id, Trigger.START, actor="system")

    async def get(self, run_id: UUID) -> Run:
        async with self._uow() as uow:
            run = await uow.runs.get(run_id)
        if run is None:
            raise NotFoundError("run", run_id)
        return run

    async def list_runs(
        self, *, states: frozenset[RunState] | None = None, limit: int = 50
    ) -> list[Run]:
        async with self._uow() as uow:
            return await uow.runs.list(states=states, limit=limit)

    async def events(self, run_id: UUID, *, limit: int = 200) -> list[Event]:
        async with self._uow() as uow:
            return await uow.events.list_for_run(run_id, limit=limit)

    async def advance(
        self,
        run_id: UUID,
        trigger: Trigger,
        *,
        actor: str,
        detail: str | None = None,
    ) -> Run:
        """Apply one trigger: transition, record an event, enqueue the next job.

        All three happen in one transaction. A crash between them is not a
        state Slipway can be in.
        """
        async with self._uow() as uow:
            run = await uow.runs.get(run_id)
            if run is None:
                raise NotFoundError("run", run_id)

            if not machine.can(run.state, trigger):
                raise IllegalTransitionError(run.state.value, trigger.value)

            target = machine.next_state(run.state, trigger)
            failure_reason = detail if target is RunState.FAILED else None
            if target is RunState.FAILED and not failure_reason:
                failure_reason = f"{trigger.value} in state {run.state.value}"

            updated = await uow.runs.update_state(
                run_id, expected=run.state, new_state=target, failure_reason=failure_reason
            )
            if updated is None:
                # Someone else moved this run between our read and our write.
                # Re-read and let the caller decide; do not clobber the winner.
                await uow.rollback()
                current = await self.get(run_id)
                raise IllegalTransitionError(current.state.value, trigger.value)

            transition_event_id = uuid7()
            await uow.events.append(
                Event(
                    id=transition_event_id,
                    run_id=run_id,
                    kind="run.transitioned",
                    created_at=datetime.now(UTC),
                    payload={
                        "from": run.state.value,
                        "to": target.value,
                        "trigger": trigger.value,
                        "actor": actor,
                        "detail": detail or "",
                    },
                )
            )

            kind = JOB_FOR_STATE.get(target)
            if kind is not None:
                await uow.jobs.enqueue(
                    Job(
                        id=uuid7(),
                        run_id=run_id,
                        kind=kind,
                        status=JobStatus.PENDING,
                        # Keyed on the transition that caused it. The
                        # transition is a compare-and-set in this same
                        # transaction, so it can happen at most once -- which
                        # makes this key unique per unit of real work, and
                        # keeps a run that loops (spec rejected, respecified,
                        # approved again) from colliding with its own history.
                        idempotency_key=f"{run_id}:{kind}:{transition_event_id}",
                        attempts=0,
                        created_at=datetime.now(UTC),
                    )
                )

            await uow.commit()

        log.info(
            "run.transitioned",
            run_id=str(run_id),
            **{"from": run.state.value},
            to=target.value,
            trigger=trigger.value,
            actor=actor,
        )
        return updated

    async def decide(
        self,
        run_id: UUID,
        gate: Gate,
        *,
        approved: bool,
        decided_by: str,
        note: str | None = None,
    ) -> Run:
        """Record a human decision at a gate and move the run.

        The (run, gate) unique constraint makes a double submission lose in the
        database rather than transition the run twice.
        """
        from app.domain.entities import Approval

        run = await self.get(run_id)
        open_gate = machine.open_gate(run.state)
        if open_gate is not gate:
            raise IllegalTransitionError(
                run.state.value, f"{'approve' if approved else 'reject'}:{gate.value}"
            )

        async with self._uow() as uow:
            await uow.approvals.record(
                Approval(
                    id=uuid7(),
                    run_id=run_id,
                    gate=gate,
                    approved=approved,
                    decided_by=decided_by,
                    decided_at=datetime.now(UTC),
                    note=note,
                )
            )
            await uow.commit()

        return await self.advance(
            run_id,
            Trigger.APPROVED if approved else Trigger.REJECTED,
            actor=decided_by,
            detail=note,
        )

    async def cancel(self, run_id: UUID, *, actor: str, reason: str) -> Run:
        return await self.advance(run_id, Trigger.CANCELLED, actor=actor, detail=reason)
