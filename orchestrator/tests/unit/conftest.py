from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.domain import ids


@pytest.fixture
def reset_id_clock() -> Iterator[None]:
    """Reset the UUIDv7 generator's monotonic state.

    The generator keeps the last millisecond it emitted at module level, so a
    test that pins the clock to a fixed value must start from a clean state or
    it inherits `_last_ms` from whatever ran before it.
    """
    ids._last_ms = -1
    ids._counter = 0
    yield
    ids._last_ms = -1
    ids._counter = 0
