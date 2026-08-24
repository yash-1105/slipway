"""SQLAlchemy Core table definitions.

These mirror orchestrator/migrations/*.sql. The SQL is the source of truth --
the schema is created by the numbered migrations, never by metadata.create_all
-- and these definitions exist so queries are typed and composable.

tests/integration/test_schema_matches_tables.py compares the two.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData()

runs = Table(
    "runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("brief", Text, nullable=False),
    Column("title", Text),
    Column("state", Text, nullable=False),
    Column("failure_reason", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

events = Table(
    "events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("kind", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

approvals = Table(
    "approvals",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("gate", Text, nullable=False),
    Column("approved", Boolean, nullable=False),
    Column("decided_by", Text, nullable=False),
    Column("note", Text),
    Column("decided_at", DateTime(timezone=True), nullable=False),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("leased_by", Text),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("run_after", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

artifacts = Table(
    "artifacts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("kind", Text, nullable=False),
    Column("uri", Text, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

deployments = Table(
    "deployments",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("artifact_uri", Text, nullable=False),
    Column("project_name", Text, nullable=False),
    Column("host", Text, nullable=False),
    Column("port", Integer, nullable=False),
    Column("url", Text),
    Column("status", Text, nullable=False),
    Column("log", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("settled_at", DateTime(timezone=True)),
)

port_allocations = Table(
    "port_allocations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("host", Text, nullable=False),
    Column("port", Integer, nullable=False),
    Column("allocated_at", DateTime(timezone=True), nullable=False),
    Column("released_at", DateTime(timezone=True)),
)

run_transitions = Table(
    "run_transitions",
    metadata,
    Column("source", Text, primary_key=True),
    Column("trigger", Text, primary_key=True),
    Column("target", Text, nullable=False),
    Column("gate", Text),
)

schema_migrations = Table(
    "schema_migrations",
    metadata,
    Column("version", Text, primary_key=True),
    Column("sha256", Text, nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=False),
)
