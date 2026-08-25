"""Entities and value objects. Pure data; no IO, no app imports."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
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
class CostEntry:
    """What one model call cost, and who it was for.

    `model_used` is what the provider says it served, which is not always what
    was asked for: a fallback fires, or the provider substitutes. The ledger
    records what happened.
    """

    id: UUID
    run_id: UUID
    role: str
    model_requested: str
    model_used: str
    prompt_tokens: int
    completion_tokens: int
    usd: Decimal
    inr: Decimal
    usd_to_inr: Decimal
    actor: str
    created_at: datetime
    job_id: UUID | None = None
    #: False when the provider served a model with no published price, so the
    #: amounts are an estimate rather than a measurement.
    priced: bool = True

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class DeploymentStatus(enum.StrEnum):
    """Where a deployment is. Mirrors the CHECK in migration 0003."""

    RECORDING = "recording"   # the row exists; the container does not yet
    LIVE = "live"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    TORN_DOWN = "torn_down"


@dataclass(frozen=True, slots=True)
class DeploymentRecord:
    """A preview deployment, as the database knows it.

    `container_name` is written before the container is created, so a crash
    mid-deploy leaves a row naming something the reconciler can look for. The
    reverse -- a container with no row -- is the case that cannot be recovered.

    `destroyed_at` is what holds the port: while it is NULL this row owns its
    port, enforced by a partial unique index rather than by anyone remembering.
    """

    id: UUID
    run_id: UUID
    status: DeploymentStatus
    host: str
    port: int
    project_name: str
    artifact_uri: str
    created_at: datetime
    container_name: str | None = None
    container_id: str | None = None
    image_tag: str | None = None
    #: The port inside the container. A sibling container on the same network
    #: addresses this; `port` is the host port a browser uses.
    container_port: int = 3000
    #: The user-defined Docker network the container is on. Without one there is
    #: no name resolution, and only the host can reach the deployment.
    network: str | None = None
    url: str | None = None
    log: str | None = None
    settled_at: datetime | None = None
    destroyed_at: datetime | None = None

    @property
    def holds_its_port(self) -> bool:
        return self.destroyed_at is None

