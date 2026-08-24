"""Run endpoints. Parse, call one service, format. No logic here."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.deps import RunServiceDep
from app.api.schemas import (
    CancelRequest,
    CreateRunRequest,
    DecisionRequest,
    EventResponse,
    RunResponse,
)
from app.domain.entities import Gate, RunState, open_gate

router = APIRouter(prefix="/runs", tags=["runs"])


@router.post("", response_model=RunResponse, status_code=201)
async def create_run(body: CreateRunRequest, runs: RunServiceDep) -> RunResponse:
    run = await runs.create(body.brief, title=body.title)
    return RunResponse.of(run, open_gate=open_gate(run.state))


@router.get("", response_model=list[RunResponse])
async def list_runs(
    runs: RunServiceDep,
    state: Annotated[list[RunState] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RunResponse]:
    found = await runs.list_runs(states=frozenset(state) if state else None, limit=limit)
    return [RunResponse.of(r, open_gate=open_gate(r.state)) for r in found]


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(run_id: UUID, runs: RunServiceDep) -> RunResponse:
    run = await runs.get(run_id)
    return RunResponse.of(run, open_gate=open_gate(run.state))


@router.get("/{run_id}/events", response_model=list[EventResponse])
async def get_run_events(
    run_id: UUID, runs: RunServiceDep, limit: Annotated[int, Query(ge=1, le=1000)] = 200
) -> list[EventResponse]:
    return [EventResponse.of(e) for e in await runs.events(run_id, limit=limit)]


@router.post("/{run_id}/gates/{gate}", response_model=RunResponse)
async def decide_gate(
    run_id: UUID, gate: Gate, body: DecisionRequest, runs: RunServiceDep
) -> RunResponse:
    run = await runs.decide(
        run_id, gate, approved=body.approved, decided_by=body.decided_by, note=body.note
    )
    return RunResponse.of(run, open_gate=open_gate(run.state))


@router.post("/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(run_id: UUID, body: CancelRequest, runs: RunServiceDep) -> RunResponse:
    run = await runs.cancel(run_id, actor=body.actor, reason=body.reason)
    return RunResponse.of(run, open_gate=open_gate(run.state))
