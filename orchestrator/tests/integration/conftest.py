"""Fixtures for tests that need a real Postgres.

`make test-integration` brings the database up first. Run directly without one
and these skip rather than fail, so a unit-test run on a laptop with no Docker
is still green.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.db.session import build_engine, build_session_factory
from app.db.uow import SqlUnitOfWork
from app.domain.repositories import UnitOfWork
from app.services.migrations import apply_pending

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

#: What the `uow_factory` fixture hands a test.
UowFactory = Callable[[], UnitOfWork]

TABLES = (
    "run_transitions",
    "port_allocations",
    "deployments",
    "artifacts",
    "jobs",
    "approvals",
    "events",
    "runs",
)


def _database_url() -> str:
    return os.environ.get(
        "SLIPWAY_TEST_DATABASE_URL",
        os.environ.get("SLIPWAY_DATABASE_URL", ""),
    )


@pytest.fixture(scope="session")
def settings() -> Settings:
    url = _database_url()
    if not url:
        pytest.skip("set SLIPWAY_DATABASE_URL (or run `make test-integration`)")
    return Settings(
        database_url=url,
        models_backend="fake",
        sandbox_backend="fake",
        deploy_backend="fake",
        artifacts_backend="memory",
    )


@pytest.fixture(scope="session")
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    created = build_engine(settings)
    try:
        async with created.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        await created.dispose()
        pytest.skip(f"no Postgres at the configured URL: {exc}")

    await apply_pending(created, MIGRATIONS)
    yield created
    await created.dispose()


@pytest.fixture
async def clean_database(engine: AsyncEngine) -> AsyncIterator[None]:
    """Empty the tables between tests.

    `events` has a DO INSTEAD NOTHING rule on DELETE, so TRUNCATE is used --
    rules do not apply to it. That is deliberate and is the only sanctioned way
    to clear the audit log; see the comment on events_no_delete in migration
    0001.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(f"TRUNCATE {', '.join(t for t in TABLES if t != 'run_transitions')} CASCADE")
        )
    yield


@pytest.fixture
def uow_factory(engine: AsyncEngine, clean_database: None) -> UowFactory:
    session_factory = build_session_factory(engine)

    def factory() -> UnitOfWork:
        return SqlUnitOfWork(session_factory)

    return factory
