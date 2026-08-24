# 0004 — Novita base URL and API surface

**Status:** Accepted
**Date:** 2026-08-24
**Settled by:** a live probe, `make models-sync`, run 2026-08-24T13:44Z

## Context

Novita's documentation shows three base paths for its OpenAI-compatible
surface:

- `https://api.novita.ai/openai/v1`
- `https://api.novita.ai/openai`
- `https://api.novita.ai/v3/openai`

The OpenAI SDK appends `/chat/completions` and `/models` to whatever `base_url`
it is given, so this had to be settled by observation. The Proposed version of
this ADR assumed **exactly one** would work and the other two would 404.

That assumption was wrong, and so was the assumption about the Responses API.
Both are recorded below because the reasoning that was wrong is the useful part.

## Evidence

### `GET {base_url}/models`

| Candidate base URL | Status | Models | Note |
| --- | --- | --- | --- |
| `https://api.novita.ai/openai/v1` | 200 | 150 | ok |
| `https://api.novita.ai/openai` | 200 | 150 | ok |
| `https://api.novita.ai/v3/openai` | 200 | 150 | ok |

All three answered. The model lists are **byte-identical** across all three —
compared as sorted id lists, not by count.

### `POST {base_url}/chat/completions`

Probed with `zai-org/glm-4.7-flash`, an id read from the list above:

| Candidate base URL | Status | Result |
| --- | --- | --- |
| `https://api.novita.ai/openai/v1` | 200 | served `zai-org/glm-4.7-flash`, 6 prompt / 1 completion tokens |
| `https://api.novita.ai/openai` | 200 | identical |
| `https://api.novita.ai/v3/openai` | 200 | identical |

The operation Slipway actually depends on works at all three.

### `POST {base_url}/responses`

This is where they differ, and it is the only place they do:

| Candidate base URL | Status | Body |
| --- | --- | --- |
| `https://api.novita.ai/openai/v1` | 400 | `INVALID_REQUEST_BODY: model: zai-org/glm-4.7-flash does not support endpoint: responses` |
| `https://api.novita.ai/openai` | 404 | `404 page not found` |
| `https://api.novita.ai/v3/openai` | 404 | `404 page not found` |

## Decision

**`https://api.novita.ai/openai/v1`.**

Not because the others fail — they do not — but because it is a strict superset.
It is the only base that exposes `/responses` at all; the other two do not route
it. Two paths that behave identically today can stop doing so, and the one that
serves more of the surface is the one less likely to be the alias that gets
retired.

`SLIPWAY_NOVITA_BASE_URL` defaults to it in `app/config.py`.

## Probe methodology: a 404 is not self-explanatory

**A 404 from a route probe made with an invalid model id is about the model, not
the route.** Read it as "the endpoint does not exist" and you will conclude the
opposite of the truth.

This cost one wrong conclusion here, which is once more than it should. Writing
it down so it does not cost a second:

- The first probe of `/responses` sent `{"model": "", ...}`. Novita answered
  `404 {"code":404,"reason":"MODEL_NOT_FOUND","message":"model not found"}`.
  That was read as "no such route". It was not: the route was there, and it was
  objecting to the empty model.
- Re-probed with a real id from the live `/models` list, the same endpoint
  answers `400 INVALID_REQUEST_BODY: model ... does not support endpoint:
  responses`. A route that exists, declining a model it does not serve.
- A genuinely missing route answers differently again — `/openai/responses` and
  `/v3/openai/responses` return `404 page not found`, plain text from the
  router, with no JSON error body and no mention of a model.

The general rule, for any endpoint probe against any provider:

1. Probe with a **valid** identifier read from the provider's own listing. An
   invalid one makes every layer of the stack a candidate for the error.
2. Distinguish the **shape** of the failure, not just the status code. A JSON
   error body naming your input came from the application; a bare
   `404 page not found` came from the router in front of it.
3. Where the provider publishes per-item capabilities — Novita's `endpoints`
   array on each model — read those instead of inferring capability from a
   probe. They are the answer; a probe only confirms it.

`scripts/sync_models.py` now probes with a real id and distinguishes
`404 page not found` from a 400, so this specific mistake cannot recur silently.

## The Responses API: support is per-model

The route exists at `/openai/v1/responses`. 7 of the 150 models list `responses`
in their `endpoints` array — among them `zai-org/glm-5.3`,
`deepseek/deepseek-v4-pro-0813` and `moonshotai/kimi-k3`. The generated
`config/models.yaml` records the full list under
`models_supporting_responses_endpoint`.

**Slipway uses chat completions only.** The reason is per-model support, not the
absence of the API:

- None of the five models this system routes to supports `responses`. Every one
  of `zai-org/glm-5.2`, `moonshotai/kimi-k2.7-code`,
  `deepseek/deepseek-v4-flash`, `zai-org/glm-4.7` and `deepseek/deepseek-v4-pro`
  is chat-completions-only.
- A client built on `responses` would work for 7 models and fail for 143. Chat
  completions works for all of them.

CLAUDE.md previously said Novita did not support the Responses API at all. It
was corrected by hand on 2026-08-24 and now states the per-model position, with
the instruction to revisit if a routed model ever advertises `responses`.

## Consequences

- `config/models.yaml` is generated by `scripts/sync_models.py` and records
  `source_base_url`, the sync timestamp, and every model id exactly as returned.
- Prices, context windows and output caps in that file are read from the
  provider's own `/models` response, never typed. A model that publishes no
  price cannot be assigned to a role: the sync refuses.
- Role assignment stays a human decision. The sync proposes matches for the
  intended families and assigns nothing on its own; `--assign` verifies both ids
  against the live list before writing them.
- Re-running the sync is how a retired model id is discovered, so it is routine
  and appears in `RUNBOOK.md` under *Provider outage*.
- `models_supporting_responses_endpoint` is recorded in the generated file, so
  if we ever want the Responses API the list of candidates is already there.

## What would reopen this

- A model we route to gaining `responses` support, which would make the choice
  between the two surfaces a real decision rather than a non-question.
- `/openai` or `/v3/openai` diverging from `/openai/v1` in any way other than
  the missing route. Today they do not.
