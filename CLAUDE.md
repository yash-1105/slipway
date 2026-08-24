# Slipway

Slipway takes a project brief, uses AI agents to write a specification, build
the application, test it and deploy it, with two human approval gates.

Internal tool, two developers. Version 1 runs on our laptops and one server,
against the Novita AI API. It is used on real client work — it is not a
prototype.

## Working method
- Every task runs in Plan Mode first. Read the code that already exists before
  proposing a change to it.
- If you do not know something, find out. Do not produce a plausible
  approximation and move on.

## Scope
- Implement only what the current prompt asks for. Nothing else.
- If you notice something missing or wrong outside that scope, write it in
  `docs/notes/observations.md` and stop. Do not build it, do not fix it, and do
  not ask to. The observation is the deliverable.
- This applies to obvious one-line fixes and to things you have just been asked
  to report on. Noticing is not permission.

## Documentation
- No document may describe behaviour that is not covered by a test. This
  applies to CLAUDE.md, ARCHITECTURE.md, RUNBOOK.md, every ADR, every README
  and every docstring.
- If you want to write down that something behaves a certain way, write the
  test first. If the behaviour is not yet built, describe it as not built, or
  leave it out.
- A command named in a runbook must exist. An invariant named in a comment must
  have a test that fails when it is violated.

## Stack (Version 1)
Python 3.11, FastAPI, SQLAlchemy 2.0 async, PostgreSQL 16, uv, pytest, ruff,
mypy strict, import-linter, structlog.
Frontend: Vite, React 19, TypeScript, Tailwind v4, TanStack Query.
Agents: LangGraph over the OpenAI SDK pointed at Novita.
NOT in V1: OpenTelemetry, Prometheus, OpenBao, OpenTofu, any vector store, any
scikit-learn model. Do not add them.

## Architecture rules
- Five seams: models, runtimes, sandbox, deploy, artifacts. Each has base.py
  with the Protocol and a factory reading an env selector. NOTHING outside a
  seam package imports a concrete implementation. Enforced by import-linter,
  not by convention.
- Layering: api/ and cli/ are thin adapters importing only services/ and
  domain/ types. services/ may use domain/, state/, agents/, integrations/ and
  seam base modules. domain/ imports nothing else from app/.
- ALL primary keys are UUIDv7, never sequential integers. Two developers run
  separate databases now and those histories must merge later.
- Env is read only in app/config.py. Config is validated at startup and the
  process refuses to boot if it is wrong.
- No hardcoded host, port or absolute path.

## Novita
- Base URL https://api.novita.ai/openai/v1 . Their docs also show /openai and
  /v3/openai. Verify with a real /models call and record it in an ADR.
- Novita does NOT support the Responses API. Chat completions only.
- Never type a model id from memory. Read it from config/models.yaml, which is
  populated from the live /models endpoint.

## Error handling
- Every external call has an explicit timeout. No defaults, no unbounded waits.
- No bare except. No `except Exception` without re-raising or a comment saying
  precisely why swallowing is correct here.
- Retries only on transient errors, with backoff and a cap. Never retry a
  non-idempotent operation without an idempotency key.
- Failures the caller must act on are VALUES, not exceptions. A failed
  container build is a BuildResult carrying the log, not a raised error.
- Record an external resource identifier BEFORE the operation completes, so a
  crash mid-call is recoverable.

## Concurrency
- Every worker job is idempotent. It will be retried; assume it.
- Job claiming uses FOR UPDATE SKIP LOCKED with a lease expiry.
- Shared resources (ports, container names, branches) are allocated through the
  database with a unique constraint, never by scanning for what looks free.
- Anything interruptible has a reconciliation loop comparing recorded state to
  actual state.

## Data
- Migrations are numbered, forward-only SQL. Never edit an applied migration.
- Constraints live in the database, not only in Python.
- The events table is append-only, enforced by a database rule with a comment.

## Testing
- tests/unit: pure logic, no IO. tests/integration: real Postgres and Docker.
  tests/acceptance: the full loop.
- A test that only asserts a mock was called is not a test.
- Never mock the thing under test. Mock its collaborators at the seam.
- Every bug fixed gets a regression test in the same PR.

## Security (V1 level)
- Secrets come from environment variables and appear in no file, including
  fixtures and seed data.
- Agent tool calls run only inside the sandbox, never in the orchestrator.
- The GitHub token is scoped to the scratch org. Never inherit a gh session.

## Never
- Add a dependency without justifying it in the commit message, and an ADR if
  it is structural.
- Weaken, skip or delete a failing test to make a suite pass.
- Invent a library API. Read the installed source or official docs first.
- Add anything from the V2 list above.

## Commands
    make dev        run Postgres, the API, the worker and the frontend
    make check      ruff, mypy strict, import-linter, pytest unit
    make migrate    apply pending forward-only SQL migrations
    make seed       insert local development fixtures
    make reset      drop and recreate the local database, then migrate
    make eval       run the eval cases in evals/cases
