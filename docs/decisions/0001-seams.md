# 0001 — Five seams behind Protocols

**Status:** Accepted
**Date:** 2026-08-24

## Context

Version 1 of Slipway is deliberately small: it runs on two laptops and one
server, calls one model provider, builds in local Docker, deploys with compose
over SSH and writes artifacts to a local disk. Every one of those five choices
is expected to be wrong within a year — the provider will change, execution
will need to be durable, sandboxing will need to be stronger than a container,
deploys will need a scheduler, artifacts will need object storage.

The usual failure here is to build V1 against the concrete tools, discover the
coupling at migration time, and rewrite. The other usual failure is to build a
general abstraction over tools we have not used yet, and get the shape wrong in
a way that costs more than the coupling would have.

## Decision

Exactly five seams: **models, runtimes, sandbox, deploy, artifacts**.

Each is a package containing:

- `base.py` — a `typing.Protocol` and the result value types it returns.
- `factory.py` — one function reading one environment selector, returning a
  concrete instance. Implementations are imported inside the function body, so
  selecting one does not import the others.
- one module per implementation.

Rules:

1. Nothing outside a seam package imports a concrete implementation. Callers
   depend on the Protocol in `base.py`.
2. This is enforced by import-linter contracts run in `make check`, not by
   convention and not by code review.
3. Seams return failures the caller must act on as **values**. A failed build
   is a `BuildResult` carrying the log, not a raised exception. Exceptions are
   for programming errors and genuinely exceptional infrastructure faults.
4. Callers pass logical intent, not vendor nouns. `ModelClient` takes a role
   ("spec", "build", "review"), not a model id.

Five, and no more. A seam is justified only where we can already name the
concrete V2 successor and say what changes at migration; `ARCHITECTURE.md`
records that for each. Anything else is a module, not a seam.

## Consequences

- Adding a sixth seam requires an ADR. This is intended friction.
- Test doubles live in the seam package and satisfy the Protocol, so unit tests
  mock collaborators at the seam and never the thing under test.
- A `Protocol` is structural, so implementations do not inherit from a base
  class and mypy strict verifies conformance at the factory's return type.
- The factory's env selector is read via `app/config.py` only, like all other
  environment access.
- Cost: an indirection on five call paths that a two-person V1 does not
  strictly need. Accepted, because the alternative is discovering the coupling
  during a migration we already know is coming.

## Alternatives considered

- **No seams; refactor when it hurts.** Rejected: all five migrations are
  known, not speculative, and the refactor would land during whichever one is
  most urgent.
- **Dependency injection container.** Rejected: a framework's worth of
  machinery to solve five constructor calls.
- **Abstract base classes.** Rejected in favour of Protocols: nothing should
  have to import a Slipway base class to be usable as an implementation,
  including a third-party object we adapt.
