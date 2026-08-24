# 0009 — The tool-calling substrate is sound; go on the agent layer

**Status:** Accepted
**Date:** 2026-08-24
**Settled by:** `scripts/bakeoff.py`, 7 candidates × 4 tasks plus a T4 repeat, $4.75

## Context

Everything above the models seam assumes a model can hold a tool loop: emit a
call that conforms to a schema, read the result, and emit the next one, for
dozens of turns. If that assumption is wrong, the agent layer does not work and
no amount of prompt engineering fixes it — a model that writes excellent code
but garbles the tool schema cannot drive this system.

That had never been measured. It was measured through a hand-written loop
(read, write, edit, bash, glob, grep) rather than LangGraph, deliberately: a
framework that repairs a malformed call is a framework that hides which model
produces them.

## Decision

**Go.** The substrate is sound.

Roughly **2 schema violations in ~900 tool calls across 7 candidates** — both a
`read` with an unknown argument. Five of the seven emitted none at all. Every
candidate authenticated, emitted well-formed calls, and drove a multi-turn loop
to a graded result.

Malformed tool call rate does not discriminate between these models. It was
expected to be the headline; it is a floor that all of them clear.

## What this does NOT prove

**Tool discipline is not build quality, and this measured the first.**

The bake-off's T4 step 5 — refactor duplicated validation into one shared
function without changing behaviour — **failed for 4 of the 7 candidates**,
including two of the three we route to. Every one of those failures came from a
model that had just emitted dozens of perfectly-formed tool calls.

Concretely, from the same run:

- `zai-org/glm-5.3` spent 25 turns on T2 making **24 `read` calls and one
  `bash`**, changed nothing, and hit the turn cap. Zero malformed calls
  throughout. Perfect schema conformance, no work done.
- `zai-org/glm-4.7` wrote three correct leap-year tests on T1, then invented
  requirements the code never had and left the suite red. Zero malformed calls.
- `zai-org/glm-5.2` returned two empty responses in a row on T1 and never
  recovered, having written nothing. Zero malformed calls.

A green malformed rate says the model can *operate* the tools. It says nothing
about whether it does the right thing with them. Anything that cites this ADR
as evidence the agents work is citing it for something it did not test.

## Consequences

- The seam, the loop and the schemas are not the risk. Build quality is.
- Model selection cannot be made on malformed rate, because it does not vary.
  It has to be made on task outcomes, which are noisier and more expensive to
  measure — two samples of T4 produced 6/6 and 5/6 from the same model at
  temperature 0.
- The failure modes worth engineering against are stalls, no-progress loops and
  out-of-scope edits, none of which is a schema problem. Carried into
  `docs/notes/p8-p9-agent-loop.md`.
- `scripts/bakeoff.py` is kept and is re-runnable. Its results are not
  committed: they are a measurement of one moment against a provider whose
  model list moves.

## What would reopen this

A routed model whose malformed rate is materially above zero, or a provider
change to how tool calls are encoded. Re-run the bake-off; it costs about $5.
