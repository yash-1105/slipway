"""Entities and value objects. Pure data; no IO, no app imports."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


class RunState(enum.StrEnum):
    """Where a run is in the pipeline.

    The legal moves between these are in app/state/machine.py and are mirrored
    by a CHECK constraint in the database.
    """

    CREATED = "created"
    SPECIFYING = "specifying"
    SPEC_REVIEW = "spec_review"          # human gate 1
    BUILDING = "building"
    TESTING = "testing"
    DEPLOY_REVIEW = "deploy_review"      # human gate 2
    DEPLOYING = "deploying"
    DEPLOYED = "deployed"                # terminal
    FAILED = "failed"                    # terminal
    CANCELLED = "cancelled"              # terminal


TERMINAL_STATES: frozenset[RunState] = frozenset(
    {RunState.DEPLOYED, RunState.FAILED, RunState.CANCELLED}
)


class Gate(enum.StrEnum):
    """The two human approval gates."""

    SPEC = "spec"
    DEPLOY = "deploy"


class Trigger(enum.StrEnum):
    """What causes a run to change state."""

    START = "start"
    AGENT_SUCCEEDED = "agent_succeeded"
    AGENT_FAILED = "agent_failed"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    DEPLOY_SUCCEEDED = "deploy_succeeded"
    DEPLOY_FAILED = "deploy_failed"


class JobStatus(enum.StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"


#: Which gate is open in a given state, if any. This is a fact about the
#: state, not about the transition table, so it lives here where an adapter can
#: read it without reaching into app/state.
GATE_FOR_STATE: dict[RunState, Gate] = {
    RunState.SPEC_REVIEW: Gate.SPEC,
    RunState.DEPLOY_REVIEW: Gate.DEPLOY,
}


def open_gate(state: RunState) -> Gate | None:
    """The gate a run in `state` is waiting at, or None if it waits on no one."""
    return GATE_FOR_STATE.get(state)


@dataclass(frozen=True, slots=True)
class Run:
    id: UUID
    brief: str
    state: RunState
    created_at: datetime
    updated_at: datetime
    title: str | None = None
    failure_reason: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


@dataclass(frozen=True, slots=True)
class Job:
    """A unit of worker work.

    `idempotency_key` is unique in the database. A job is claimed with
    FOR UPDATE SKIP LOCKED and held under `lease_expires_at`; if the worker
    dies the lease expires and another worker re-claims it, which is why every
    job body must be idempotent.
    """

    id: UUID
    run_id: UUID
    kind: str
    status: JobStatus
    idempotency_key: str
    attempts: int
    created_at: datetime
    lease_expires_at: datetime | None = None
    leased_by: str | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class Event:
    """An append-only audit record. The gates are judged against these."""

    id: UUID
    run_id: UUID
    kind: str
    created_at: datetime
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Approval:
    """A human decision at a gate. One decision per (run, gate)."""

    id: UUID
    run_id: UUID
    gate: Gate
    approved: bool
    decided_by: str
    decided_at: datetime
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """A stored artifact, addressed by URI with an explicit scheme.

    `file://` in V1, `s3://` in V2; see docs/decisions/0001-seams.md.
    """

    id: UUID
    run_id: UUID
    kind: str
    uri: str
    sha256: str
    size_bytes: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PortAllocation:
    """A port on the deploy host, owned by exactly one run.

    Allocated by inserting a row and letting the unique constraint arbitrate,
    never by scanning for a port that looks free.
    """

    id: UUID
    run_id: UUID
    host: str
    port: int
    allocated_at: datetime
    released_at: datetime | None = None
