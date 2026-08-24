# Observations

Things noticed while doing something else. Nothing here is a task and nothing
here has been acted on — per the scope rule in CLAUDE.md, noticing is not
permission. Each entry records what was seen, how it was confirmed, and what it
would cost to be wrong.

Format: newest first. When one is picked up in a prompt, mark it Resolved with
the commit that did it. Do not delete entries.

---

## 2026-08-24 — "Each is a GitHub template repository" is not achievable as written

**Resolved.** ADR 0008 settles it: blueprints stay directories, instantiated by
copying the tracked subtree and running `git init`. `slipway-blueprints/README.md`
now describes that flow.

`slipway-blueprints/README.md` says: "One directory per archetype. Each is a
**GitHub template repository**." A directory inside a repository cannot be a
GitHub template repository — the template flag is a property of a repository,
so either each blueprint is its own repo (and `slipway-blueprints` is an index,
not a container), or blueprints are directories and instantiation is a copy
rather than GitHub's "Use this template".

Noticed while satisfying the nextjs-console definition of done, which said
"create a repo from this template". It was instantiated by copying the tracked
tree and running `git init`, which is what the directory layout supports. That
produced a working repo and all four commands passed, but it is not the GitHub
template flow the README describes.

Nothing was changed. Which way this resolves affects how the builder agent
scaffolds a run and how a generated repo records which blueprint version it
came from, so it is a decision rather than a typo.

## 2026-08-24 — Documents describing behaviour that does not exist

**Resolved**, all three. Item 1 by writing the test rather than deleting the
claim — it found real drift on its first run. Items 2 and 3 by correcting the
documents, since the code they described would have been new features.
`scripts/check_runbook_commands.py` now fails `make check` if the runbook
prescribes a command the CLI does not have.

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

**Still open.** The related problem — the deploy job handing a build log to the
deployer as if it were a bundle — was fixed separately: the stage now refuses
when no artifact of kind `bundle` exists. Nothing still writes a `deployments`
row, so the crash-mid-deploy case below is unchanged.

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

**Partly addressed.** The stub is unchanged. `RUNBOOK.md` no longer claims
rollback works; it says plainly that it is not implemented and gives the manual
teardown instead.

It returns `DeployFailure("unreachable", ...)` whose message points at
`services.deploys.rollback`. There is no `app/services/deploys.py`. The
`RUNBOOK.md` rollback procedure depends on both.

## 2026-08-24 — `reconcile.apply()` is untested

**Still open.** `RUNBOOK.md` now warns about it at the point of use.

`inspect()` has tests. `apply()` — the half that stops containers, tears down
deployments and releases ports — has 0% coverage, measured with `pytest --cov`.
It is reachable today only through `slipway reconcile --apply`. Cost: the
destructive path is the one with no test.
