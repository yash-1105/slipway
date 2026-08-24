"""The forward-only migration runner.

Applies every numbered file in orchestrator/migrations/ that has not been
applied yet, in order, each in its own transaction, recording its sha256.

An applied migration whose file has since changed is a hard error: migrations
are forward-only and editing one that has run anywhere makes two databases
silently disagree.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.domain.errors import SlipwayError

log = structlog.get_logger(__name__)

_FILENAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text        PRIMARY KEY,
    sha256      char(64)    NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE schema_migrations IS
    'Which numbered migrations this database has. sha256 pins the file '
    'contents so an edited-after-applying migration is caught, not ignored.';
"""


class MigrationError(SlipwayError):
    """A migration is missing, out of order, or was edited after being applied."""


async def _execute_script(conn: AsyncConnection, sql: str) -> None:
    """Run a multi-statement SQL script.

    asyncpg sends anything with parameters through the extended query protocol,
    which accepts exactly one statement -- so `conn.execute(text(script))` fails
    with "cannot insert multiple commands into a prepared statement" on every
    migration we have. Dropping to the driver connection uses the simple query
    protocol, which is what a migration file needs.

    The surrounding `engine.begin()` still wraps it, so a migration that fails
    partway leaves nothing behind.
    """
    raw = await conn.get_raw_connection()
    driver_connection = raw.driver_connection
    if driver_connection is None:  # pragma: no cover -- an engine.begin() always has one
        raise RuntimeError("no driver connection behind this AsyncConnection")
    await driver_connection.execute(sql)


def discover(directory: Path) -> list[tuple[str, Path]]:
    """Return (version, path) pairs in numeric order, rejecting bad names."""
    found: list[tuple[str, Path]] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if match is None:
            raise MigrationError(
                f"{path.name} does not match NNNN_lowercase_name.sql; "
                "migrations are numbered so their order is not a guess"
            )
        found.append((match.group(1), path))

    versions = [v for v, _ in found]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration numbers in {directory}: {versions}")
    return found


async def apply_pending(engine: AsyncEngine, directory: Path) -> list[str]:
    """Apply everything not yet applied. Returns the versions applied."""
    async with engine.begin() as conn:
        await _execute_script(conn, _BOOTSTRAP)

    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT version, sha256 FROM schema_migrations"))).all()
    applied = {version: digest for version, digest in rows}

    pending: list[str] = []
    for version, path in discover(directory):
        content = path.read_text()
        digest = hashlib.sha256(content.encode()).hexdigest()

        if version in applied:
            if applied[version] != digest:
                raise MigrationError(
                    f"migration {version} ({path.name}) has changed since it was applied. "
                    "Migrations are forward-only. Add a new one instead of editing this."
                )
            continue

        # Each migration is its own transaction, so a failure leaves the
        # earlier ones applied and this one not, which is a recoverable state.
        async with engine.begin() as conn:
            await _execute_script(conn, content)
            await conn.execute(
                text(
                    "INSERT INTO schema_migrations (version, sha256, applied_at) "
                    "VALUES (:v, :s, :a)"
                ),
                {"v": version, "s": digest, "a": datetime.now(UTC)},
            )
        log.info("migration.applied", version=version, file=path.name)
        pending.append(version)

    return pending


class MigrationService:
    """What `slipway migrate` and `make migrate` call."""

    def __init__(self, engine: AsyncEngine, directory: Path) -> None:
        self._engine = engine
        self._directory = directory

    async def apply_pending(self) -> list[str]:
        return await apply_pending(self._engine, self._directory)

    def discover(self) -> list[tuple[str, Path]]:
        return discover(self._directory)
