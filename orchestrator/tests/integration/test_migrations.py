"""The migration runner and the constraints the migrations install."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.services.migrations import MigrationError, discover
from tests.integration.conftest import MIGRATIONS

pytestmark = pytest.mark.integration


def test_migrations_are_numbered_and_ordered() -> None:
    versions = [version for version, _ in discover(MIGRATIONS)]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))


def test_a_badly_named_migration_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "add_a_column.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="numbered"):
        discover(tmp_path)


async def test_applying_twice_is_a_no_op(engine: AsyncEngine) -> None:
    """The runner is idempotent; `make migrate` is safe to repeat."""
    from app.services.migrations import apply_pending

    assert await apply_pending(engine, MIGRATIONS) == []


async def test_an_edited_applied_migration_is_refused(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """Forward-only means an applied file may never change."""
    from app.services.migrations import apply_pending

    for _, path in discover(MIGRATIONS):
        (tmp_path / path.name).write_text(path.read_text())
    edited = sorted(tmp_path.glob("*.sql"))[0]
    edited.write_text(edited.read_text() + "\n-- a later edit\n")

    with pytest.raises(MigrationError, match="forward-only"):
        await apply_pending(engine, tmp_path)


async def test_the_events_table_refuses_updates(engine: AsyncEngine, clean_database: None) -> None:
    """Append-only, enforced by a rule -- not by everyone remembering."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runs (id, brief, state) "
                "VALUES ('01900000-0000-7000-8000-000000000001', 'brief', 'created')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO events (id, run_id, kind) VALUES "
                "('01900000-0000-7000-8000-000000000002', "
                "'01900000-0000-7000-8000-000000000001', 'run.created')"
            )
        )
        await conn.execute(text("UPDATE events SET kind = 'tampered'"))

        kind = (await conn.execute(text("SELECT kind FROM events"))).scalar_one()

    assert kind == "run.created", "the UPDATE must have been discarded"


async def test_the_events_table_refuses_deletes(engine: AsyncEngine, clean_database: None) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runs (id, brief, state) "
                "VALUES ('01900000-0000-7000-8000-000000000003', 'brief', 'created')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO events (id, run_id, kind) VALUES "
                "('01900000-0000-7000-8000-000000000004', "
                "'01900000-0000-7000-8000-000000000003', 'run.created')"
            )
        )
        await conn.execute(text("DELETE FROM events"))

        remaining = (await conn.execute(text("SELECT count(*) FROM events"))).scalar_one()

    assert remaining == 1, "the DELETE must have been discarded"


async def test_an_unknown_run_state_is_refused_by_the_database(
    engine: AsyncEngine, clean_database: None
) -> None:
    """Constraints live in the database, not only in Python."""
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO runs (id, brief, state) "
                    "VALUES ('01900000-0000-7000-8000-000000000005', 'brief', 'daydreaming')"
                )
            )


async def test_a_failed_run_must_carry_a_reason(engine: AsyncEngine, clean_database: None) -> None:
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO runs (id, brief, state) "
                    "VALUES ('01900000-0000-7000-8000-000000000006', 'brief', 'failed')"
                )
            )


async def test_a_non_failed_run_may_not_carry_a_reason(
    engine: AsyncEngine, clean_database: None
) -> None:
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO runs (id, brief, state, failure_reason) VALUES "
                    "('01900000-0000-7000-8000-000000000007', 'brief', 'created', 'why?')"
                )
            )


async def test_schema_migrations_records_what_was_applied(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT version, sha256 FROM schema_migrations"))).all()

    versions = {version for version, _ in rows}
    assert versions == {version for version, _ in discover(MIGRATIONS)}
    assert all(len(digest) == 64 for _, digest in rows)


async def test_migrations_never_create_tables_outside_the_runner(engine: AsyncEngine) -> None:
    """metadata.create_all is never used; the SQL is the source of truth."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY table_name"
            )
        )
        tables = {row[0] for row in result}

    assert {
        "runs", "events", "approvals", "jobs", "artifacts",
        "deployments", "port_allocations", "run_transitions", "schema_migrations",
    } <= tables


async def test_the_partial_unique_index_on_ports_allows_reuse_after_release(
    engine: AsyncEngine, clean_database: None
) -> None:
    run = "01900000-0000-7000-8000-000000000008"
    async with engine.begin() as conn:
        await conn.execute(
            text(f"INSERT INTO runs (id, brief, state) VALUES ('{run}', 'brief', 'created')")
        )
        await conn.execute(
            text(
                "INSERT INTO port_allocations (id, run_id, host, port) VALUES "
                f"('01900000-0000-7000-8000-000000000009', '{run}', 'h', 41000)"
            )
        )

    # A second live allocation of the same port loses.
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO port_allocations (id, run_id, host, port) VALUES "
                    f"('01900000-0000-7000-8000-00000000000a', '{run}', 'h', 41000)"
                )
            )

    # Once released, the port is free again.
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE port_allocations SET released_at = now()"))
        await conn.execute(
            text(
                "INSERT INTO port_allocations (id, run_id, host, port) VALUES "
                f"('01900000-0000-7000-8000-00000000000b', '{run}', 'h', 41000)"
            )
        )
        live = (
            await conn.execute(
                text("SELECT count(*) FROM port_allocations WHERE released_at IS NULL")
            )
        ).scalar_one()

    assert live == 1
