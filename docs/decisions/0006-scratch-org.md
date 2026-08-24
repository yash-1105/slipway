# 0006 — Generated repositories live in a scratch org

**Status:** Accepted
**Date:** 2026-08-24

> Written to match the reference already in `slipway-blueprints/README.md`.
> The org itself was created by hand and is not managed by this repository.

## Context

The builder agent creates a repository per run and pushes generated code to it.
Two things about that are uncomfortable.

The first is blast radius. A token that can create repositories under a
developer's account can also push to every repository that developer can reach —
including this one, including client work that has nothing to do with the run.
An agent with a bug, or a brief carrying a prompt injection, then has write
access to everything.

The second is noise. A pipeline that produces a repository per run will produce
hundreds, mixed in among real projects, with no way to tell at a glance which
are disposable.

## Decision

Generated repositories are created in a separate GitHub organisation — the
**scratch org** — and nowhere else.

- The org exists solely to hold generated output. Nothing in it is precious and
  everything in it is disposable.
- Slipway authenticates to it with a token scoped to that org and to nothing
  else, supplied as `SLIPWAY_GITHUB_TOKEN` with `SLIPWAY_GITHUB_ORG`.
- That token is **never** a developer's `gh` session. `app/integrations/github.py`
  takes the token from configuration and refuses to construct a client without
  one; it does not fall back to an ambient credential, because a fallback that
  silently works is a fallback that silently escalates.
- Slipway's own source — this repository and `slipway-blueprints` — is not in
  the scratch org. Those are not generated and are not disposable.

## Consequences

- The worst case for a compromised or misbehaving agent is a mess inside a
  namespace that exists to be messy.
- Cleaning up is a namespace-wide operation rather than a hunt.
- The token needs rotating separately from anything else, which is the point.
- Cost: two GitHub namespaces to think about, and a token that has to be
  provisioned before the first real run.

## Alternatives considered

- **A single account with a fine-grained personal access token.** Rejected: the
  scoping is per-repository, and the repositories do not exist until the agent
  creates them.
- **One long-lived repository with a branch per run.** Rejected: branches share
  history, settings and CI minutes, and a generated push cannot be isolated from
  the others.
