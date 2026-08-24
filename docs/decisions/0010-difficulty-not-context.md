# 0010 — Difficulty degrades quality; context length did not

**Status:** Accepted
**Date:** 2026-08-24
**Settled by:** `scripts/bakeoff.py` task T4, 7 candidates, two runs of the leaders

## Context

T4 exists to answer one question: does a model get worse as a session grows?
Six small dependent tasks in one conversation, each with its own check, run
after that step. The builder does exactly this, so the answer shapes how work
is decomposed.

The expected answer was that quality decays with context — the common
justification for "one task per session".

## Evidence

Steps, and how many of the 7 candidates passed each:

| Step | Passed |
| --- | --- |
| 1 — add `slugify` to helpers | 7/7 |
| 2 — use it in the route | 7/7 |
| 3 — add a `validate_tags` validator | 7/7 |
| 4 — test the slug | 5/7 |
| **5 — refactor duplicated validation, behaviour unchanged** | **3/7** |
| 6 — write a CHANGELOG and confirm the build | 6/7 |

Splitting each T4 session's tool calls into thirds and counting schema
violations and references to things that do not exist:

| Model | Calls | early | mid | late |
| --- | --- | --- | --- | --- |
| glm-5.2 | 58 | 0/0 | 0/0 | 0/1 |
| kimi-k2.7-code | 33 | 0/0 | 0/0 | 0/0 |
| deepseek-v4-flash | 58 | 0/1 | 0/0 | 0/0 |
| glm-4.7 | 39 | 0/0 | 0/0 | 0/0 |
| glm-5.3 | 52 | 0/0 | 0/0 | 0/0 |
| deepseek-v4-pro | 61 | 0/0 | 0/0 | 0/0 |
| glm-4.7-flash | 53 | 0/0 | 1/0 | 0/0 |

## Decision

**Task difficulty degrades quality. Context length, over a session of this
size, did not.**

The last step passed 6 of 7. The hardest step passed 3 of 7. There is no drift
in schema discipline from the start of a session to the end.

**This invalidates the context-decay justification for "one task per
session".** That rule stands, on different grounds:

- **Retry granularity.** A failed session retries one task, not six.
- **PR reviewability.** One task is a diff a human can hold in their head.
- **Audit trail.** A run's events map to units of work a person can name.

None of those is about context windows, and stating the rule in terms of decay
would have made it look refutable by a longer context window. It is not.

**The real lever is task decomposition quality**, which is a planner concern.
If a step is beyond the model, it fails whether it is first or sixth; the
planner's job is to not produce steps like step 5. That is now a known,
measured property to design the planner against rather than a suspicion.

## Consequences

- Do not spend effort shortening agent sessions to avoid context decay. It was
  not observed at this scale.
- Do spend effort on decomposition: a step like "refactor these without
  changing behaviour" is the failure case, and the planner should either split
  it or hand it to a model that passed it.
- `deepseek/deepseek-v4-flash` was the only candidate to pass step 5 in both
  runs. That is the strongest single signal in the bake-off and is why it is
  now primary for `test_author` and `doc_writer`.
- This says nothing about sessions much longer than T4's ~50 turns. The claim
  is bounded by what was measured.

## What would reopen this

A session an order of magnitude longer than T4, or a model whose context window
is materially smaller than the ~200k these have. Re-run T4 with more steps.
