"""UUIDv7 (RFC 9562). See docs/decisions/0003-uuidv7-identifiers.md."""

from __future__ import annotations

import uuid

import pytest

from app.domain import ids
from app.domain.ids import timestamp_ms, uuid7


def test_version_and_variant_bits_are_rfc_9562() -> None:
    for _ in range(1000):
        value = uuid7()
        assert value.version == 7
        # Variant bits must be 0b10 -- RFC 9562 section 4.1.
        assert (value.int >> 62) & 0b11 == 0b10


def test_timestamp_is_the_first_48_bits(reset_id_clock: None) -> None:
    value = uuid7(_ms=1_700_000_000_123)
    assert timestamp_ms(value) == 1_700_000_000_123


def test_timestamp_ms_rejects_other_uuid_versions() -> None:
    with pytest.raises(ValueError, match="not a UUIDv7"):
        timestamp_ms(uuid.uuid4())


def test_ids_are_unique() -> None:
    assert len({uuid7() for _ in range(50_000)}) == 50_000


def test_ids_sort_in_creation_order() -> None:
    """The whole reason for v7 over v4: index locality and sortability."""
    values = [uuid7() for _ in range(50_000)]
    assert values == sorted(values)


def test_monotonic_within_a_single_millisecond(reset_id_clock: None) -> None:
    """Many ids in one millisecond still sort, via the rand_a counter."""
    values = [uuid7(_ms=1_700_000_000_000) for _ in range(500)]
    assert values == sorted(values)
    assert len(set(values)) == 500
    assert all(timestamp_ms(v) == 1_700_000_000_000 for v in values)


def test_counter_overflow_borrows_the_next_millisecond(reset_id_clock: None) -> None:
    """More than 4096 ids in one millisecond must still be ordered.

    The generator borrows the following millisecond rather than emitting an id
    that sorts before one it already returned.
    """
    values = [uuid7(_ms=1_700_000_000_000) for _ in range(9_000)]
    assert values == sorted(values)
    assert len(set(values)) == 9_000
    assert timestamp_ms(values[-1]) > 1_700_000_000_000


def test_clock_going_backwards_does_not_break_ordering(reset_id_clock: None) -> None:
    """An NTP step or a suspend/resume must not produce a descending id."""
    forward = uuid7(_ms=1_700_000_000_000)
    backward = uuid7(_ms=1_600_000_000_000)  # the clock jumped a year into the past
    assert backward > forward
    assert timestamp_ms(backward) >= timestamp_ms(forward)


def test_generator_state_is_module_local(reset_id_clock: None) -> None:
    """Sanity check on the fixture that isolates these tests from each other."""
    assert ids._last_ms == -1
