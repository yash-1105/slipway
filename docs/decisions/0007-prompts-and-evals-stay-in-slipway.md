# 0007 — Prompts and evals stay in this repository

**Status:** Accepted
**Date:** 2026-08-24

> Written to match the reference already in `slipway-blueprints/README.md`.

## Context

There are two repositories that could reasonably hold the agent prompts:

- **`slipway`** — the orchestrator that reads them.
- **`slipway-blueprints`** — the archetype scaffolds the builder agent works in,
  each with its own `AGENTS.md` describing that scaffold.

The pull towards blueprints is real: a prompt is largely about the scaffold it
is producing code for, and a blueprint already carries scaffold-specific
instructions.

The problem is versioning. A run's output is a function of the prompt that
produced it. When a gate rejects a specification and someone asks what the agent
was told, the answer has to be exact and it has to be recoverable months later.
If the prompt lives in a second repository on its own timeline, answering that
question means correlating two histories by timestamp, which is a guess.

The same applies to evals: an eval result is only meaningful against the prompt
version it ran on.

## Decision

`prompts/` and `evals/` live in `slipway`, versioned with the orchestrator that
reads them.

- A run records the orchestrator version it ran under, which pins the prompt
  text exactly, because they move in the same commit.
- A prompt change and the eval case that justifies it land in one diff and can
  be reviewed together.
- `slipway-blueprints` holds scaffolds and their `AGENTS.md` — instructions
  about *a scaffold*, which belong with the scaffold and change with it.

The dividing line: if it describes how an agent behaves, it is in `slipway`. If
it describes the shape of the thing being built, it is in `slipway-blueprints`.

## Consequences

- A prompt change is a Slipway release. That is intended friction: prompts are
  the most behaviour-changing thing in the system and the least type-checked.
- `prompts/` is mounted read-only into the API and worker containers, so a
  deployed orchestrator cannot be edited into a different one.
- Blueprint-specific instruction lives in that blueprint's `AGENTS.md`, so the
  prompts here stay about method rather than about any one scaffold.
- Cost: touching a prompt to suit one blueprint means a change in this
  repository. Accepted, because the alternative is a prompt whose history
  cannot be reconstructed from a run id.
