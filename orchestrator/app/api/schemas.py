"""Request and response shapes for the HTTP API.

Separate from domain entities on purpose: the wire format is allowed to change
for the frontend's convenience without dragging the domain with it.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.entities import Event, Gate, Run, RunState


class CreateRunRequest(BaseModel):
    brief: str = Field(min_length=1, max_length=100_000)
    title: str | None = Field(default=None, max_length=200)


class DecisionRequest(BaseModel):
    approved: bool
    decided_by: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=5_000)


class CancelRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=5_000)


class RunResponse(BaseModel):
    id: UUID
    brief: str
    title: str | None
    state: RunState
    failure_reason: str | None
    open_gate: Gate | None
    is_terminal: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, run: Run, *, open_gate: Gate | None) -> RunResponse:
        return cls(
            id=run.id,
            brief=run.brief,
            title=run.title,
            state=run.state,
            failure_reason=run.failure_reason,
            open_gate=open_gate,
            is_terminal=run.is_terminal,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )


class EventResponse(BaseModel):
    id: UUID
    kind: str
    created_at: datetime
    payload: dict[str, object]

    @classmethod
    def of(cls, event: Event) -> EventResponse:
        return cls(
            id=event.id, kind=event.kind, created_at=event.created_at, payload=event.payload
        )


class HealthResponse(BaseModel):
    status: str
    database: bool
    models_catalogue_synced_at: str
