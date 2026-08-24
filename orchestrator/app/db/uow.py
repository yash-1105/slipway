"""A unit of work: one transaction spanning the repositories."""

from __future__ import annotations

from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories import (
    SqlApprovalRepository,
    SqlArtifactRepository,
    SqlCostRepository,
    SqlDeploymentRepository,
    SqlEventRepository,
    SqlJobRepository,
    SqlRunRepository,
)


class SqlUnitOfWork:
    """Implements app.domain.repositories.UnitOfWork.

    Exiting without an explicit commit rolls back. That is deliberate: a job
    that crashed halfway should leave nothing behind, because it is going to be
    retried from the top.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> SqlUnitOfWork:
        self._session = self._session_factory()
        session = self._session
        self.runs = SqlRunRepository(session)
        self.events = SqlEventRepository(session)
        self.jobs = SqlJobRepository(session)
        self.approvals = SqlApprovalRepository(session)
        self.artifacts = SqlArtifactRepository(session)
        self.costs = SqlCostRepository(session)
        self.deployments = SqlDeploymentRepository(session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        session = self._require_session()
        try:
            if exc is not None:
                await session.rollback()
        finally:
            await session.close()
            self._session = None

    async def commit(self) -> None:
        await self._require_session().commit()

    async def rollback(self) -> None:
        await self._require_session().rollback()

    def _require_session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("SqlUnitOfWork used outside `async with`")
        return self._session
