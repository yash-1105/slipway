"""Ports are allocated through the database, never by scanning for a free one."""

from __future__ import annotations

import asyncio

import pytest

from app.domain.errors import ResourceExhaustedError
from app.domain.ids import uuid7
from app.services.ports import PortAllocator
from tests.unit.fakes import InMemoryStore, InMemoryUnitOfWork

HOST = "deploy.internal"


def allocator(store: InMemoryStore, *, start: int = 41000, end: int = 41002) -> PortAllocator:
    return PortAllocator(lambda: InMemoryUnitOfWork(store), start=start, end=end)


async def test_the_first_allocation_takes_the_bottom_of_the_range() -> None:
    store = InMemoryStore()
    allocation = await allocator(store).allocate(uuid7(), host=HOST)
    assert allocation.port == 41000


async def test_two_runs_never_get_the_same_port() -> None:
    store = InMemoryStore()
    ports = [(await allocator(store).allocate(uuid7(), host=HOST)).port for _ in range(3)]
    assert sorted(ports) == [41000, 41001, 41002]


async def test_concurrent_allocations_do_not_collide() -> None:
    """The constraint arbitrates. Losing the race is normal, not an error."""
    store = InMemoryStore()
    allocations = await asyncio.gather(
        *(allocator(store).allocate(uuid7(), host=HOST) for _ in range(3))
    )
    assert len({a.port for a in allocations}) == 3


async def test_an_exhausted_range_is_reported_not_guessed_at() -> None:
    store = InMemoryStore()
    for _ in range(3):
        await allocator(store).allocate(uuid7(), host=HOST)

    with pytest.raises(ResourceExhaustedError, match="reconcile"):
        await allocator(store).allocate(uuid7(), host=HOST)


async def test_releasing_a_port_returns_it_to_the_range() -> None:
    store = InMemoryStore()
    first = await allocator(store).allocate(uuid7(), host=HOST)
    await allocator(store).release(first.id)

    second = await allocator(store).allocate(uuid7(), host=HOST)
    assert second.port == first.port
    assert second.id != first.id


async def test_hosts_have_independent_ranges() -> None:
    store = InMemoryStore()
    here = await allocator(store).allocate(uuid7(), host=HOST)
    there = await allocator(store).allocate(uuid7(), host="other.internal")
    assert here.port == there.port == 41000
