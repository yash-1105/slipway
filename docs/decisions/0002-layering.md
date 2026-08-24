# 0002 — Layering, and the direction of dependency

**Status:** Accepted
**Date:** 2026-08-24

## Context

Slipway has two entry points into the same logic: an HTTP API used by the
frontend, and a CLI used by the two of us for operations — the procedures in
`RUNBOOK.md` are CLI commands. It also has a worker with no entry point at all.

If business logic accumulates in request handlers, the CLI cannot reuse it and
grows a second, divergent copy; the runbook then documents behaviour that the
API does not have. This is the specific failure mode being designed out.

## Decision

Four layers, dependencies pointing downward only:

    api/  cli/          adapters
    services/          business logic
    agents/  state/  integrations/
    domain/            entities, repository Protocols, pure logic

- **`api/` and `cli/`** are thin adapters. They parse input, call one service,
  and format output. They may import `services/` and `domain/` types, plus the
  two modules that sit structurally above everything — `app/config.py` and
  `app/container.py`, the composition root — and nothing else from `app/`. In
  particular they may not import `agents/`, `state/`, `integrations/`, `db/`,
  or any seam: an adapter that reaches a seam directly is business logic
  wearing a router's clothes. `slipway models ping` goes through
  `services/models.py` for exactly that reason.
- **`services/`** holds the business logic. It may use `domain/`, `state/`,
  `agents/`, `integrations/` and seam `base.py` modules.
- **`domain/`** imports nothing else from `app/`. Entities, value objects, the
  repository Protocols that `services/` depends on and infrastructure
  implements, and pure functions over them. It is the layer that is trivially
  unit-testable because it cannot do IO.
- **`app/config.py`** is the only module that reads the environment, and is
  therefore allowed to be imported from anywhere.

Enforced by import-linter contracts in `orchestrator/pyproject.toml`: a layers
contract for the direction, plus explicit forbidden contracts for the rules a
layers contract alone cannot express (adapters skipping past `services/` into
lower layers, and `domain/` importing anything).

Two details of that enforcement matter before you edit the contracts:

- The forbidden contracts set `allow_indirect_imports = true`. Without it,
  import-linter follows the chain `container -> factory -> impl` and reports the
  container -- but reaching an implementation *through* the factory is the whole
  design. The rule being enforced is about direct imports.
- Modules named on one line of a `layers` contract are siblings and may not
  import each other, which is why `main`/`worker`, then `api`/`cli`, then
  `container` are three separate layers rather than one.

The one rule in CLAUDE.md that import-linter cannot express is "env is read only
in `app/config.py`": `os.environ` is an attribute, not a module. That half is
enforced by `scripts/check_env_access.py`, which `make check` runs.

## Consequences

- Every runbook procedure is a service call with a CLI adapter, so the API can
  expose the same operation later without reimplementation.
- The worker is an adapter too: it claims jobs and calls services.
- `domain/` having no `app/` imports means the state transition table, the UUIDv7
  generator and the entities are testable in `tests/unit` with no fixtures.
- Repository Protocols live in `domain/` while their SQLAlchemy implementations
  live in infrastructure, so `services/` depends on the Protocol and the
  database session never appears in a service signature.
- Cost: some operations are a one-line service wrapping a one-line repository
  call. Accepted; the alternative is deciding case by case, which ends with the
  CLI and the API disagreeing.

## Alternatives considered

- **Adapters call repositories directly for simple reads.** Rejected: "simple"
  is not a stable property, and the exception is what erodes the rule.
- **A single `core/` package.** Rejected: it makes the seam and layering rules
  unenforceable, since import-linter needs distinct packages to reason about.
