# Observations

Things noticed while doing something else. Nothing here is a task and nothing
here has been acted on — per the scope rule in CLAUDE.md, noticing is not
permission. Each entry records what was seen, how it was confirmed, and what it
would cost to be wrong.

Format: newest first. When one is picked up in a prompt, mark it Resolved with
the commit that did it. Do not delete entries.

---

## 2026-08-24 — Documents describing behaviour that does not exist

Found while producing the module inventory. All three predate the
"no document may describe behaviour that is not covered by a test" rule, and
all three now violate it.

**1. `orchestrator/app/db/tables.py` cites a test that does not exist.**
Its docstring says `tests/integration/test_schema_matches_tables.py compares the
two`. There is no such file. Nothing checks the SQLAlchemy Core table
definitions against the SQL migrations. Confirmed by `ls tests/integration/`.
Cost of being wrong: the Core definitions drift from the schema and queries
fail at runtime against a database the migrations produced correctly.

**2. `ARCHITECTURE.md` claims LangGraph is "checkpointed to Postgres".**
`app/runtimes/impl/langgraph_local.py` configures no checkpointer. Confirmed by
grep for `checkpoint` across the runtimes seam: no match. Cost: the sentence
implies a worker can resume a graph mid-node. It cannot; it replays the job.

**3. `RUNBOOK.md` documents seven commands that do not exist.**
`runs unstick`, `runs pause`, `runs resume`, `workers list`, `budget show`,
`deploys list`, `deploys rollback`. The CLI has ten commands; the runbook
describes seventeen. Confirmed by comparing `@app.command` decorators in
`app/cli/main.py` against the runbook. Cost: an operator follows the runbook
during an incident and the command is not there.

## 2026-08-24 — The `deployments` table has no writes

Migration `0003_deploy.sql` comments that status `'recording'` is written
*before* the compose project is created, so a crash mid-deploy leaves a row the
reconciler can find. Nothing writes to the table. Confirmed by grepping `app/`
for `deployments` outside `db/tables.py`: only `reconcile.py`, and only its own
dataclass field of the same name.

The port allocator does honour record-before-operate. The deployment does not.
Cost: a crash between `deployer.deploy()` starting and finishing leaves a
compose project on the server that no row in our database refers to — the one
case `ARCHITECTURE.md` names as unrecoverable.

## 2026-08-24 — Budget caps are read by nothing

`SLIPWAY_BUDGET_DAILY_USD_CAP` and `SLIPWAY_BUDGET_RUN_USD_CAP` are validated
at startup and then never referenced. Confirmed by grep across `app/`: matches
only in `config.py`. `RUNBOOK.md`'s "Budget exhausted" procedure and the
`budget_exhausted` variants in `CompletionFailure` and `GraphFailure` have no
producer. Cost: two developers share one Novita key with no ceiling.

## 2026-08-24 — The sandbox seam is wired but never invoked

`app/sandbox/impl/docker.py` is 127 lines of container confinement that has
never executed. Nothing outside `app/sandbox/` calls `start()` or `exec()` —
confirmed by grep. The agent graphs in `app/agents/graphs.py` are single model
calls returning free text; no tool use, so nothing needs a sandbox yet.

This is expected at this point in the build plan (P6/P9), and is recorded only
so the 0% coverage is not mistaken for an oversight. Cost of forgetting: the
first agent that needs a tool call has an untested sandbox underneath it.

## 2026-08-24 — `compose_ssh.rollback()` is a stub citing a module that does not exist

It returns `DeployFailure("unreachable", ...)` whose message points at
`services.deploys.rollback`. There is no `app/services/deploys.py`. The
`RUNBOOK.md` rollback procedure depends on both.

## 2026-08-24 — `reconcile.apply()` is untested

`inspect()` has tests. `apply()` — the half that stops containers, tears down
deployments and releases ports — has 0% coverage, measured with `pytest --cov`.
It is reachable today only through `slipway reconcile --apply`. Cost: the
destructive path is the one with no test.
