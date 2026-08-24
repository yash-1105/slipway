"""Readiness. Lives in services/ so the router stays an adapter."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models.base import ModelCatalogue


@dataclass(frozen=True, slots=True)
class Health:
    database: bool
    models_catalogue_synced_at: str

    @property
    def status(self) -> str:
        return "ok" if self.database else "degraded"


class HealthService:
    def __init__(self, engine: AsyncEngine, catalogue: ModelCatalogue, *, timeout_seconds: float):
        self._engine = engine
        self._catalogue = catalogue
        self._timeout = timeout_seconds

    async def check(self) -> Health:
        database_ok = True
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except SQLAlchemyError:
            # Swallowed deliberately: a health check reports the database as
            # down, it does not fail. A monitor needs the shape of the answer
            # to stay stable even when the answer is bad.
            database_ok = False

        return Health(
            database=database_ok,
            models_catalogue_synced_at=self._catalogue.synced_at,
        )
