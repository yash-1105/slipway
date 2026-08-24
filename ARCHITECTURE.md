# Slipway architecture

One orchestrator process family (API + worker), one Postgres, one sandbox host,
one deploy target. A run moves a brief through spec, build, test and deploy,
pausing at two human approval gates.

    brief ──▶ SPECIFYING ──▶ [GATE 1: spec approved] ──▶ BUILDING ──▶ TESTING
                                                                        │
              DEPLOYED ◀── DEPLOYING ◀── [GATE 2: deploy approved] ◀────┘

The API never does work. It writes rows and enqueues jobs. The worker claims
jobs with `FOR UPDATE SKIP LOCKED` under a lease and executes them. Every job
is idempotent because every job will, eventually, be retried.

## Layering

    api/  cli/            thin adapters; services/ and domain/ types only
      └── services/       business logic; may use domain, state, agents,
                          integrations and seam base modules
            ├── agents/   LangGraph graphs
            ├── state/    the transition table
            ├── integrations/
            └── domain/   entities and repository protocols; imports nothing
                          else from app/

`app/config.py` is the only module that reads the environment. It validates at
import and the process refuses to boot on bad configuration.

Enforced by import-linter contracts in `orchestrator/pyproject.toml`, run by
`make check`. Not by convention, and not by review.

## The five seams

Each seam package is exactly three things: `base.py` holding a Protocol and the
result value types, `factory.py` reading one env selector and returning a
concrete instance, and one module per implementation. Nothing outside the seam
package may import an implementation — callers depend on the Protocol.

Failures that a caller must act on cross a seam as values, not exceptions. A
failed container build returns a `BuildResult` carrying the log.

### models — talking to an LLM
- **V1:** Novita, through the OpenAI SDK's chat completions API. Model ids are
  read from `config/models.yaml`, which is generated from the live `/models`
  endpoint; none is ever typed from memory. Novita has no Responses API.
- **V2:** a router across providers with per-run cost accounting and failover.
- **At migration:** the selector changes and a second implementation appears.
  Callers already pass a logical role ("spec", "build", "review"), not a model
  id, so nothing above the seam changes.

### runtimes — executing an agent graph
- **V1:** LangGraph executed in the worker process, checkpointed to Postgres.
- **V2:** a durable execution engine so a graph survives a worker restart
  mid-node rather than replaying the job from its last committed step.
- **At migration:** jobs are already idempotent and leased, so the replay
  semantics V2 removes are the ones V1 relies on. The Protocol does not change.

### sandbox — running agent tool calls
- **V1:** a Docker container per run from `sandbox-image/`, no network except
  an allowlist, workspace mounted, resource-capped.
- **V2:** a pool of microVMs on the server, pre-warmed, with snapshot restore.
- **At migration:** callers hold an opaque `SandboxHandle` recorded in the
  database before the container is created, so a crash mid-create is
  reconcilable either way. Only `factory.py` learns the new name.

### deploy — publishing the built application
- **V1:** `docker compose` over SSH to the one server, one compose project per
  run. Ports are allocated by inserting into a table with a unique constraint,
  never by scanning for a free one.
- **V2:** a scheduler (Kubernetes or Nomad) with real rollouts and rollback.
- **At migration:** the port allocator becomes unnecessary and its table is
  retired; `DeployResult` already carries a URL and an opaque deployment id, so
  the reconciliation loop keeps working against a different backend.

### artifacts — storing specs, logs, bundles and images
- **V1:** local filesystem under a configured root, addressed by URI, with
  metadata rows in Postgres.
- **V2:** an S3-compatible object store.
- **At migration:** artifacts are already URI-addressed with an explicit scheme
  (`file://` today, `s3://` later) and content-hashed, so old rows stay valid
  and resolution is a scheme lookup inside the seam.

## Data

All primary keys are UUIDv7, generated in Python. Two developers run separate
databases today and those histories must merge later; sequential integers would
collide and UUIDv4 would destroy index locality.

Migrations are numbered, forward-only SQL under `orchestrator/migrations/`. An
applied migration is never edited. Constraints live in the database — the state
machine's legal transitions, the port uniqueness, the one-approval-per-gate
rule. The `events` table is append-only, enforced by a rule with a comment
explaining it.

## What V1 deliberately does not have

No OpenTelemetry, no Prometheus, no OpenBao, no OpenTofu, no vector store, no
learned model. Observability in V1 is structlog to stdout plus the `events`
table, which is the audit log the approval gates are judged against. Adding any
of the above is a V2 decision and needs an ADR.
