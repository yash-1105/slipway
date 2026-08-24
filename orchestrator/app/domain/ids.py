"""UUIDv7 identifiers (RFC 9562 section 5.7).

All Slipway primary keys are UUIDv7, generated here, in the application. See
docs/decisions/0003-uuidv7-identifiers.md for why: two developers run separate
databases whose histories must merge later, and identifiers must exist before
the external operation that uses them completes.

`uuid.uuid7()` lands in the Python 3.14 standard library. When we are on 3.14
this module becomes a delegation to it and the tests below it stay unchanged.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

# RFC 9562 section 6.2, "Monotonic Random", method 1: the 12 bits of `rand_a`
# hold a counter that increments for each id generated within the same
# millisecond, so ids created in one millisecond in one process still sort in
# creation order.
_MAX_COUNTER = 0xFFF

_lock = threading.Lock()
_last_ms = -1
_counter = 0


def uuid7(*, _ms: int | None = None) -> uuid.UUID:
    """Return a new UUIDv7.

    `_ms` overrides the clock and exists for tests; production callers pass
    nothing.
    """
    global _last_ms, _counter

    ms = _now_ms() if _ms is None else _ms

    with _lock:
        if ms > _last_ms:
            _last_ms = ms
            # Seed the counter low so there is room to increment within the
            # millisecond without overflowing into a borrowed millisecond.
            _counter = int.from_bytes(os.urandom(2), "big") & 0x3FF
        else:
            # Same millisecond, or the clock went backwards (NTP step,
            # suspend/resume). Either way, keep emitting monotonically from
            # the last millisecond we used.
            _counter += 1
            if _counter > _MAX_COUNTER:
                # Out of counter space. Borrow the next millisecond rather
                # than emit a non-monotonic id.
                _last_ms += 1
                _counter = 0
            ms = _last_ms

        counter = _counter

    value = (ms & 0xFFFFFFFFFFFF) << 80          # 48 bits: unix_ts_ms
    value |= 0x7 << 76                            # 4 bits: version
    value |= (counter & _MAX_COUNTER) << 64       # 12 bits: rand_a (counter)
    value |= 0b10 << 62                           # 2 bits: variant
    value |= int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)  # rand_b

    return uuid.UUID(int=value)


def timestamp_ms(value: uuid.UUID) -> int:
    """Extract the millisecond timestamp from a UUIDv7."""
    if value.version != 7:
        raise ValueError(f"not a UUIDv7: {value} (version {value.version})")
    return value.int >> 80


def _now_ms() -> int:
    return time.time_ns() // 1_000_000
