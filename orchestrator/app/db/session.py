"""Engine and session construction.

Built from Settings by the composition root. Nothing here reads the
environment.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings


def build_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        pool_pre_ping=True,
        connect_args={
            # Explicit timeouts on every external call, including this one.
            "timeout": settings.database_connect_timeout_seconds,
            "server_settings": {
                "statement_timeout": str(
                    int(settings.database_statement_timeout_seconds * 1000)
                ),
                "application_name": "slipway",
            },
        },
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
