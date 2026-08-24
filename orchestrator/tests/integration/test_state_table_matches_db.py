"""app/state/machine.py and the run_transitions table must not drift.

The transition table exists in two places on purpose -- Python decides, SQL can
be queried and constrains -- so something has to fail when they disagree. This
is that something.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.entities import RunState, open_gate
from app.state import machine

pytestmark = pytest.mark.integration


async def test_the_transition_tables_are_identical(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT source, trigger, target, gate FROM run_transitions"))
        ).all()

    in_database = {(source, trigger): (target, gate) for source, trigger, target, gate in rows}
    in_python = {
        (t.source.value, t.trigger.value): (t.target.value, t.gate.value if t.gate else None)
        for t in machine.TRANSITIONS
    }

    missing_from_database = set(in_python) - set(in_database)
    missing_from_python = set(in_database) - set(in_python)

    assert not missing_from_database, (
        f"in app/state/machine.py but not in migrations: {sorted(missing_from_database)}. "
        "Add a migration."
    )
    assert not missing_from_python, (
        f"in the database but not in app/state/machine.py: {sorted(missing_from_python)}"
    )
    assert in_python == in_database, "same keys, different targets or gates"


async def test_every_state_the_machine_knows_passes_the_check_constraint(
    engine: AsyncEngine, clean_database: None
) -> None:
    """A state Python can reach but the CHECK rejects is a run that cannot be saved."""
    from app.domain.ids import uuid7

    async with engine.begin() as conn:
        for state in RunState:
            run_id = uuid7()
            reason = "'because'" if state is RunState.FAILED else "NULL"
            await conn.execute(
                text(
                    f"INSERT INTO runs (id, brief, state, failure_reason) "
                    f"VALUES ('{run_id}', 'brief', '{state.value}', {reason})"
                )
            )


async def test_the_gate_column_agrees_with_the_domain(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT target, gate FROM run_transitions WHERE gate IS NOT NULL")
            )
        ).all()

    for target, gate in rows:
        assert open_gate(RunState(target)) is not None
        assert open_gate(RunState(target)).value == gate  # type: ignore[union-attr]
