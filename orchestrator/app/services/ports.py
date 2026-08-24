"""Allocating a port on the deploy host.

Ports are claimed by inserting a row and letting the partial unique index
arbitrate. Nothing here asks the operating system what looks free -- two
workers asking that question at the same moment get the same answer.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import structlog

from app.domain.entities import PortAllocation
from app.domain.errors import ResourceExhaustedError
from app.domain.repositories import UnitOfWork

log = structlog.get_logger(__name__)

UowFactory = Callable[[], UnitOfWork]


class PortAllocator:
    def __init__(self, uow_factory: UowFactory, *, start: int, end: int) -> None:
        self._uow = uow_factory
        self._start = start
        self._end = end

    async def allocate(self, run_id: UUID, *, host: str) -> PortAllocation:
        """Claim the lowest free port in the range.

        Walks upward, letting the unique constraint reject a port that another
        worker took between our read and our write. Losing that race is normal.
        """
        async with self._uow() as uow:
            taken = {a.port for a in await uow.ports.list_active(host=host)}

            for port in range(self._start, self._end + 1):
                if port in taken:
                    continue
                allocation = await uow.ports.allocate(run_id=run_id, host=host, port=port)
                if allocation is not None:
                    await uow.commit()
                    log.info(
                        "port.allocated", run_id=str(run_id), host=host, port=port
                    )
                    return allocation
                # Someone else won this port in the moment between the read
                # above and the insert. Try the next one.

            await uow.rollback()

        raise ResourceExhaustedError(
            f"no free port on {host} in range {self._start}-{self._end}; "
            "release stale allocations with `slipway reconcile --apply`"
        )

    async def release(self, allocation_id: UUID) -> None:
        async with self._uow() as uow:
            await uow.ports.release(allocation_id)
            await uow.commit()
        log.info("port.released", allocation_id=str(allocation_id))
