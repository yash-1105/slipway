"""Properties of the transition table, not a restatement of it."""

from __future__ import annotations

import pytest

from app.domain.entities import TERMINAL_STATES, Gate, RunState, Trigger, open_gate
from app.domain.errors import IllegalTransitionError
from app.state import machine


def test_every_state_is_reachable_from_created() -> None:
    """A state nothing can reach is dead code with a CHECK constraint."""
    reachable = {RunState.CREATED}
    frontier = [RunState.CREATED]
    while frontier:
        state = frontier.pop()
        for trigger in machine.triggers_from(state):
            target = machine.next_state(state, trigger)
            if target not in reachable:
                reachable.add(target)
                frontier.append(target)

    assert reachable == set(RunState), f"unreachable: {set(RunState) - reachable}"


def test_terminal_states_have_no_way_out() -> None:
    for state in TERMINAL_STATES:
        assert machine.triggers_from(state) == frozenset()
        assert machine.is_terminal(state)


def test_every_non_terminal_state_can_be_cancelled() -> None:
    """An operator must be able to stop a run wherever it is (RUNBOOK.md)."""
    for state in RunState:
        if state in TERMINAL_STATES:
            continue
        assert machine.can(state, Trigger.CANCELLED)
        assert machine.next_state(state, Trigger.CANCELLED) is RunState.CANCELLED


def test_the_two_gates_are_the_only_states_that_wait_on_a_human() -> None:
    gated = {state for state in RunState if open_gate(state) is not None}
    assert gated == {RunState.SPEC_REVIEW, RunState.DEPLOY_REVIEW}
    assert open_gate(RunState.SPEC_REVIEW) is Gate.SPEC
    assert open_gate(RunState.DEPLOY_REVIEW) is Gate.DEPLOY


def test_rejecting_a_gate_goes_back_a_step_rather_than_failing_the_run() -> None:
    """Rejection is the gate working, not the run breaking."""
    assert machine.next_state(RunState.SPEC_REVIEW, Trigger.REJECTED) is RunState.SPECIFYING
    assert machine.next_state(RunState.DEPLOY_REVIEW, Trigger.REJECTED) is RunState.BUILDING


def test_the_happy_path_end_to_end() -> None:
    path = [
        (RunState.CREATED, Trigger.START, RunState.SPECIFYING),
        (RunState.SPECIFYING, Trigger.AGENT_SUCCEEDED, RunState.SPEC_REVIEW),
        (RunState.SPEC_REVIEW, Trigger.APPROVED, RunState.BUILDING),
        (RunState.BUILDING, Trigger.AGENT_SUCCEEDED, RunState.TESTING),
        (RunState.TESTING, Trigger.AGENT_SUCCEEDED, RunState.DEPLOY_REVIEW),
        (RunState.DEPLOY_REVIEW, Trigger.APPROVED, RunState.DEPLOYING),
        (RunState.DEPLOYING, Trigger.DEPLOY_SUCCEEDED, RunState.DEPLOYED),
    ]
    for source, trigger, expected in path:
        assert machine.next_state(source, trigger) is expected


def test_an_illegal_move_names_both_the_state_and_the_trigger() -> None:
    with pytest.raises(IllegalTransitionError) as caught:
        machine.next_state(RunState.CREATED, Trigger.APPROVED)
    assert "created" in str(caught.value)
    assert "approved" in str(caught.value)


def test_the_table_has_no_duplicate_source_trigger_pairs() -> None:
    """Two rows with the same key would make one of them silently unreachable."""
    keys = [(t.source, t.trigger) for t in machine.TRANSITIONS]
    assert len(keys) == len(set(keys))


def test_a_gate_is_recorded_on_the_transition_that_opens_it() -> None:
    gated = {t.target: t.gate for t in machine.TRANSITIONS if t.gate is not None}
    assert gated == {RunState.SPEC_REVIEW: Gate.SPEC, RunState.DEPLOY_REVIEW: Gate.DEPLOY}
