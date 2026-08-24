# Observations

Things noticed while doing something else. Nothing here is a task and nothing
here has been acted on — per the scope rule in CLAUDE.md, noticing is not
permission. Each entry records what was seen, how it was confirmed, and what it
would cost to be wrong.

Format: newest first. When one is picked up in a prompt, mark it Resolved with
the commit that did it. Do not delete entries.

---

## 2026-08-24 — CLAUDE.md's Responses API claim is false

CLAUDE.md states: *"Novita does NOT support the Responses API. Chat completions
only."*

The route exists at `https://api.novita.ai/openai/v1/responses`, and 7 of the
150 models list `responses` in their `endpoints` array. Probing it with a real
model id returns `400 INVALID_REQUEST_BODY: model ... does not support endpoint:
responses` — a route that exists rejecting a model. Only `/openai` and
`/v3/openai` return `404 page not found`, which is a route that does not.
Evidence is in ADR 0004.

**Nothing was changed in response to it, and nothing needs to be urgently.**
Slipway still uses chat completions, correctly: none of the five models it
routes to supports `responses`, and support is per-model, so a client built on
it would work for 7 models and fail for 143.

What is wrong is the sentence, not the behaviour. The rule it is written under —
"no document may describe behaviour that is not covered by a test" — now applies
to CLAUDE.md itself. The fix is one sentence, and it is a decision about the
project's own constitution rather than a code change, so it is recorded here
rather than made.

## 2026-08-24 — Fallback models were chosen, not specified

The prompt named the five primaries. It did not name fallbacks, and every role
needs one. These were chosen and are **unconfirmed**:

| Role | Primary | Fallback | Why this fallback |
| --- | --- | --- | --- |
| planner | `zai-org/glm-5.2` | `deepseek/deepseek-v4-pro` | different vendor, same 1M context |
| builder | `moonshotai/kimi-k2.7-code` | `zai-org/glm-5.2` | different vendor, strong on code |
| evaluator | `deepseek/deepseek-v4-flash` | `zai-org/glm-4.7-flash` | different vendor, both cheap |
| test_author | `zai-org/glm-4.7` | `deepseek/deepseek-v4-flash` | different vendor, cheaper |
| doc_writer | `zai-org/glm-4.7` | `deepseek/deepseek-v4-flash` | different vendor, cheaper |

The rule applied was: a different vendor where one exists, since a rate limit or
a capacity problem usually hits one family at a time; and a context window no
smaller than the caller sized its prompt for. Changing any of them is one
`--assign` and costs nothing.

Also worth a decision: `zai-org/glm-5.3` exists, is the same price as 5.2
(\$1.4/\$4.4 per Mtok), has the same 1M context, and is the newer model. It was
not substituted, because the prompt said GLM 5.2.
---

## 2026-08-24 — P4 stopped at the live probe: no Novita key

**Resolved.** The key was supplied from the keychain; the probe ran, `config/models.yaml` is populated from the live endpoint and ADR 0004 is Accepted.

`make models-sync`, moving ADR 0004 to Accepted, and the definition of done for
this prompt (probe table, ledger rows from one real call per role, a forced
fallback against the real provider) all need `SLIPWAY_NOVITA_API_KEY`. It is not
in the environment, not in any dotfile, and not in the keychain.

The router, the cost ledger and the sync script are built and tested. What is
not done, and cannot be without the key:

- `config/models.yaml` has no model ids, so no role is assigned. That is the
  designed state: the models seam refuses to start and names the unconfigured
  roles. It is not a placeholder to be filled in from memory.
- ADR 0004 stays **Proposed**. The base URL is still unverified.
- Whether Novita exposes a Responses API is still a documented claim rather than
  an observation. `scripts/sync_models.py` probes `/responses` and reports it.

Nothing was invented to work around this. The intended families are recorded in
`scripts/sync_models.py` as families in a person's words -- "GLM 5.2" -- which
is not a model id; the script proposes matches from what the endpoint returns
and a human confirms.
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
