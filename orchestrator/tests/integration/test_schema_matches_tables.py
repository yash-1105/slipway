"""app/db/tables.py must match the schema the migrations actually produce.

The SQL is the source of truth: the schema is created by the numbered
migrations and never by `metadata.create_all`. The Core definitions exist so
queries are typed and composable, which means they are a second copy of the
same facts -- and a second copy with nothing comparing it to the first drifts.

When it drifts, the failure is a query that is valid Python, passes mypy, and
raises at runtime against a database the migrations built correctly. This is
the test the docstring in app/db/tables.py promises.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Table, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import tables as t

pytestmark = pytest.mark.integration

#: Built once. SQLAlchemy does not annotate the dialect constructor.
_PG: Dialect = postgresql.dialect()  # type: ignore[no-untyped-call]

#: DDL type -> the `data_type` string information_schema reports for it.
#: Only the types this schema uses; an unmapped one fails loudly rather than
#: being skipped, because a silently-skipped column is the drift this test
#: exists to catch.
_EXPECTED_SQL_TYPE: dict[str, str] = {
    "BIGINT": "bigint",
    "BOOLEAN": "boolean",
    "CHAR": "character",
    "INTEGER": "integer",
    "JSONB": "jsonb",
    "NUMERIC": "numeric",
    "TEXT": "text",
    "TIMESTAMP WITH TIME ZONE": "timestamp with time zone",
    "TIMESTAMP WITHOUT TIME ZONE": "timestamp without time zone",
    "UUID": "uuid",
    "VARCHAR": "character varying",
}


async def _columns(engine: AsyncEngine, table: str) -> dict[str, tuple[str, bool]]:
    """name -> (data_type, is_nullable) as Postgres reports it."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :table"
                ),
                {"table": table},
            )
        ).all()
    return {name: (data_type, nullable == "YES") for name, data_type, nullable in rows}


def _declared_type(column: Column[object]) -> str:
    """The DDL type a Core column compiles to under the Postgres dialect.

    Compiled rather than read off `str(column.type)`: the generic form renders
    `DateTime(timezone=True)` as `DATETIME`, which is not a Postgres type and
    silently loses the timezone that distinguishes the two candidates.
    """
    compiled = column.type.compile(dialect=_PG).upper()
    # Drop any length: `CHAR(64)` -> `CHAR`. information_schema reports the
    # length separately, and this test compares types, not widths.
    return compiled.split("(")[0].strip()


@pytest.mark.parametrize("table", sorted(t.metadata.tables), ids=str)
async def test_every_declared_table_exists(engine: AsyncEngine, table: str) -> None:
    columns = await _columns(engine, table)
    assert columns, (
        f"app/db/tables.py declares {table!r} but no migration creates it. "
        "Either add the migration or remove the Table."
    )


@pytest.mark.parametrize("table", sorted(t.metadata.tables), ids=str)
async def test_declared_columns_match_the_migrations(engine: AsyncEngine, table: str) -> None:
    declared: Table = t.metadata.tables[table]
    actual = await _columns(engine, table)

    missing = sorted(c.name for c in declared.columns if c.name not in actual)
    assert not missing, (
        f"{table}: app/db/tables.py declares columns the schema does not have: "
        f"{missing}. A query selecting one of these is valid Python that fails "
        "at runtime."
    )

    for column in declared.columns:
        actual_type, actual_nullable = actual[column.name]
        expected_base = _declared_type(column)

        assert expected_base in _EXPECTED_SQL_TYPE, (
            f"{table}.{column.name} uses {expected_base}, which this test does "
            "not know how to compare. Add it to _EXPECTED_SQL_TYPE rather than "
            "leaving the column unchecked."
        )
        assert actual_type == _EXPECTED_SQL_TYPE[expected_base], (
            f"{table}.{column.name}: declared {expected_base} "
            f"({_EXPECTED_SQL_TYPE[expected_base]}), schema has {actual_type}"
        )

        # A column the Core definition thinks is NOT NULL, but which the schema
        # allows to be null, produces rows that violate the Python types every
        # caller was written against.
        assert column.nullable == actual_nullable, (
            f"{table}.{column.name}: declared "
            f"{'nullable' if column.nullable else 'NOT NULL'}, schema has "
            f"{'nullable' if actual_nullable else 'NOT NULL'}"
        )


@pytest.mark.parametrize("table", sorted(t.metadata.tables), ids=str)
async def test_no_column_exists_that_is_not_declared(engine: AsyncEngine, table: str) -> None:
    """A column added by a migration and not mirrored here is invisible to queries.

    Not fatal the way the reverse is -- nothing breaks, the column is simply
    unreachable -- but it means a migration landed without the code that uses
    it, which is worth knowing before someone reimplements the column.
    """
    declared = {c.name for c in t.metadata.tables[table].columns}
    actual = set(await _columns(engine, table))

    undeclared = sorted(actual - declared)
    assert not undeclared, (
        f"{table}: the schema has columns app/db/tables.py does not declare: "
        f"{undeclared}. They cannot be selected through the Core definitions."
    )


async def test_the_primary_keys_are_uuid_not_integers(engine: AsyncEngine) -> None:
    """ADR 0003: all primary keys are UUIDv7, never sequential integers.

    A migration adding a `serial` primary key would satisfy every check above,
    because tables.py would be updated to match it.
    """
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT c.table_name, c.column_name, c.data_type
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage k
                      ON k.constraint_name = tc.constraint_name
                     AND k.table_schema = tc.table_schema
                    JOIN information_schema.columns c
                      ON c.table_schema = k.table_schema
                     AND c.table_name = k.table_name
                     AND c.column_name = k.column_name
                    WHERE tc.constraint_type = 'PRIMARY KEY'
                      AND tc.table_schema = 'public'
                    """
                )
            )
        ).all()

    assert rows, "no primary keys found; the query is wrong, not the schema"

    # run_transitions and schema_migrations are keyed by their natural text
    # keys, which is correct: neither is an entity that has to merge across two
    # developers' databases.
    text_keyed = {
        ("run_transitions", "source"),
        ("run_transitions", "trigger"),
        ("schema_migrations", "version"),
    }

    for table_name, column_name, data_type in rows:
        if (table_name, column_name) in text_keyed:
            assert data_type == "text"
            continue
        assert data_type == "uuid", (
            f"{table_name}.{column_name} is {data_type}, not uuid. "
            "See docs/decisions/0003-uuidv7-identifiers.md."
        )
