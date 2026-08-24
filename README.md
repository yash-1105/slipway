# Slipway

Takes a project brief, uses AI agents to write a specification, build the
application, test it and deploy it, with two human approval gates.

Internal tool, two developers, Version 1. It runs on our laptops and one
server, against the Novita AI API, and it is used on real client work.

```
brief ──▶ SPECIFYING ──▶ [GATE 1: spec] ──▶ BUILDING ──▶ TESTING
                                                            │
          DEPLOYED ◀── DEPLOYING ◀── [GATE 2: deploy] ◀─────┘
```

## Read these first

| | |
| --- | --- |
| `CLAUDE.md` | How work is done here, and the rules that are not negotiable |
| `ARCHITECTURE.md` | The five seams, what they are now, what replaces them in V2 |
| `RUNBOOK.md` | Stuck run, dead worker, orphan cleanup, outage, budget, rollback |
| `docs/decisions/` | Why things are the way they are |

## Getting started

```sh
make install          # Python and frontend dependencies
make dev              # Postgres, the API, the worker and the frontend
make check            # ruff, mypy strict, import-linter, env access, unit tests
```

A fresh checkout runs against the fake seams, so it boots with no credentials.
Export `SLIPWAY_NOVITA_API_KEY` and the models seam switches to Novita; export
`SLIPWAY_DEPLOY_SSH_HOST` and the deploy seam switches to compose-over-SSH.

Before the first real run:

```sh
export SLIPWAY_NOVITA_API_KEY=...
make models-sync      # populates config/models.yaml from the live /models endpoint
```

No model id is ever typed from memory. See
`docs/decisions/0004-novita-base-url.md`.

## Layout

```
orchestrator/     the API, the worker and the CLI
  app/config.py   the only module that reads the environment
  app/domain/     entities and repository protocols; imports nothing else
  app/services/   business logic
  app/state/      the run transition table
  app/{models,runtimes,sandbox,deploy,artifacts}/   the five seams
  migrations/     numbered, forward-only SQL
frontend/         Vite, React 19, TypeScript, Tailwind v4, TanStack Query
sandbox-image/    the container agent tool calls run in
prompts/          agent prompts, as files, so a change shows up in a diff
evals/            cases pinning down what the agents must produce
deploy/           compose and deploy.sh for the one V1 server
```

The layering and the seam boundaries are enforced by import-linter in
`make check`, not by convention.
