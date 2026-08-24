"""The run state transition table.

This is the whole state machine. It is a table, not a pile of `if` statements,
because it is also the thing the database CHECK constraint and the frontend
mirror, and because a table can be enumerated in a test.

Mirrored by migration 0002's `run_transition` table. If you change this, add a
migration; the two are compared by tests/integration/test_state_table_matches_db.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.domain.entities import (
    TERMINAL_STATES,
    Gate,
    RunState,
    Trigger,
)
from app.domain.entities import open_gate as open_gate
from app.domain.errors import IllegalTransitionError


@dataclass(frozen=True, slots=True)
class Transition:
    source: RunState
    trigger: Trigger
    target: RunState
    #: Set when arriving at `target` means waiting for a human.
    gate: Gate | None = None


TRANSITIONS: Final[tuple[Transition, ...]] = (
    # --- specification -----------------------------------------------------
    Transition(RunState.CREATED, Trigger.START, RunState.SPECIFYING),
    Transition(RunState.SPECIFYING, Trigger.AGENT_SUCCEEDED, RunState.SPEC_REVIEW, Gate.SPEC),
    Transition(RunState.SPECIFYING, Trigger.AGENT_FAILED, RunState.FAILED),
    # Gate 1. Rejection sends the brief back for another specification pass;
    # it does not fail the run, because rejecting a spec is the gate working.
    Transition(RunState.SPEC_REVIEW, Trigger.APPROVED, RunState.BUILDING),
    Transition(RunState.SPEC_REVIEW, Trigger.REJECTED, RunState.SPECIFYING),
    # --- build and test ----------------------------------------------------
    Transition(RunState.BUILDING, Trigger.AGENT_SUCCEEDED, RunState.TESTING),
    Transition(RunState.BUILDING, Trigger.AGENT_FAILED, RunState.FAILED),
    Transition(RunState.TESTING, Trigger.AGENT_SUCCEEDED, RunState.DEPLOY_REVIEW, Gate.DEPLOY),
    Transition(RunState.TESTING, Trigger.AGENT_FAILED, RunState.FAILED),
    # Gate 2. Rejection here goes back to BUILDING: the spec is agreed, the
    # build is not.
    Transition(RunState.DEPLOY_REVIEW, Trigger.APPROVED, RunState.DEPLOYING),
    Transition(RunState.DEPLOY_REVIEW, Trigger.REJECTED, RunState.BUILDING),
    # --- deploy ------------------------------------------------------------
    Transition(RunState.DEPLOYING, Trigger.DEPLOY_SUCCEEDED, RunState.DEPLOYED),
    Transition(RunState.DEPLOYING, Trigger.DEPLOY_FAILED, RunState.FAILED),
    # --- cancellation ------------------------------------------------------
    # Allowed from every non-terminal state, including mid-agent: the worker
    # checks for cancellation between nodes.
    *(
        Transition(state, Trigger.CANCELLED, RunState.CANCELLED)
        for state in RunState
        if state not in TERMINAL_STATES
    ),
)

_BY_KEY: Final[dict[tuple[RunState, Trigger], Transition]] = {
    (t.source, t.trigger): t for t in TRANSITIONS
}

def next_state(source: RunState, trigger: Trigger) -> RunState:
    """Return the state reached from `source` on `trigger`.

    Raises IllegalTransitionError if the table has no such move. This is a
    programming error -- a caller that does not know whether a move is legal
    should ask `can(...)` first.
    """
    transition = _BY_KEY.get((source, trigger))
    if transition is None:
        raise IllegalTransitionError(source.value, trigger.value)
    return transition.target


def can(source: RunState, trigger: Trigger) -> bool:
    return (source, trigger) in _BY_KEY


def triggers_from(source: RunState) -> frozenset[Trigger]:
    return frozenset(trigger for (state, trigger) in _BY_KEY if state == source)


def is_terminal(state: RunState) -> bool:
    return state in TERMINAL_STATES


