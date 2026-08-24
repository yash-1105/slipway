# 0003 — UUIDv7 primary keys, generated in-process

**Status:** Accepted
**Date:** 2026-08-24

## Context

Two developers run separate local databases today, and one server runs a third.
Those histories have to be mergeable later — exported runs, events and
artifacts from one database must be loadable into another without renumbering.
Sequential integers make that impossible without a rewrite pass that breaks
every recorded foreign key and every external identifier we handed to Docker,
compose or GitHub.

UUIDv4 solves collisions but destroys index locality: every insert lands in a
random leaf of the primary key B-tree, and our highest-volume table (`events`,
append-only) is exactly the case that punishes it.

UUIDv7 (RFC 9562) is a 48-bit Unix millisecond timestamp followed by random
bits, so it is globally unique, k-sortable, and inserts at the right edge of
the index.

## Decision

All primary keys are UUIDv7, generated in the application, not the database.

- Generated in `app/domain/ids.py`, hand-written, ~30 lines, no dependency.
  `uuid.uuid7()` arrives in the Python 3.14 standard library; when we are on
  3.14 this module becomes a one-line delegation and the tests stay.
- Generated in Python rather than by a Postgres default so that an identifier
  can be recorded *before* the operation that uses it completes — a container
  name or a compose project id exists in our row before it exists in Docker,
  which is what makes a crash mid-call reconcilable.
- Monotonic within a millisecond: the generator keeps a counter in the
  `rand_a` field per RFC 9562 §6.2 method 1, so ids created in the same
  millisecond in the same process still sort in creation order.
- Stored as Postgres `uuid`, not `text`.

## Consequences

- Rows can be created and referenced in one transaction without a round trip.
- Exports merge by union. There is no id remapping step, ever.
- Ids leak a creation timestamp. Accepted for an internal tool; no Slipway
  identifier is a capability or a secret.
- We own ~30 lines of bit-twiddling. Mitigated by unit tests covering version
  and variant bits, timestamp extraction, ordering across and within a
  millisecond, and the clock-regression case.

## Alternatives considered

- **The `uuid-utils` or `uuid7` package.** Rejected: a compiled or unmaintained
  dependency for a function the standard library is about to absorb, on a
  project whose rule is that dependencies need justification.
- **Postgres-side generation** (`gen_random_uuid()`, or a v7 SQL function).
  Rejected: it forces a round trip before the id exists, which conflicts
  directly with recording external identifiers before the operation completes.
- **ULID.** Rejected: same ordering properties, but it is not a `uuid` to
  Postgres, to SQLAlchemy, or to anything we would export to.
