# 0008 — Blueprints stay directories in one repository

**Status:** Accepted
**Date:** 2026-08-24

## Context

`slipway-blueprints` holds one archetype per directory. Its README described
each of those directories as "a GitHub template repository", which cannot be
true: the template flag is a property of a repository, not of a path inside
one. So the layout was already one of two things and the documentation named
the other.

The two options are real:

- **Directories in one repository.** Instantiation is a copy of a subtree.
- **One repository per archetype.** Instantiation is GitHub's "Use this
  template", which gives a fresh repository with no history.

P1 verified the first: `git archive HEAD blueprints/nextjs-console` piped into
a clean directory, then `git init`, produces a working repository whose
install, build, smoke test and container build all pass.

## Decision

Blueprints are directories inside `slipway-blueprints`. They do not become
separate GitHub repositories.

The scaffolder instantiates one by copying the tracked subtree and running
`git init` in the copy. It does not use GitHub's template feature, and no
blueprint directory carries a template flag, because a directory cannot.

Three reasons, in the order they matter:

1. **A blueprint is not one file.** `blueprint.yaml`, the schema it must
   satisfy, `AGENTS.md`, and the constitution tests that make `AGENTS.md`
   enforceable are four things that only mean anything together. Splitting them
   across repositories means a schema change is one pull request per archetype,
   each of which can be merged separately, and the window between the first and
   the last is a window where blueprints disagree about what a blueprint is.
   In one repository it is one pull request, and `npm test` walks every
   blueprint, so an archetype that has not been updated fails immediately
   rather than at plan time.

2. **Two archetypes do not justify N repositories.** The cost of the split is
   paid per archetype, immediately; the benefit — independent versioning,
   separate access control — is speculative and would only start to matter at a
   number of archetypes we are nowhere near. If we reach that number this ADR
   is superseded, and the migration is mechanical: `git subtree split` per
   directory.

3. **Every extra repository widens the scratch token's scope surface.** The
   token Slipway uses is scoped to the scratch org (ADR 0006), and the
   discipline that makes that worth anything is keeping the number of things it
   can reach small and enumerable. A repository per archetype turns one grant
   into a growing list, each of which is another thing to review when the token
   is rotated and another thing to forget to remove.

## Consequences

- `slipway-blueprints/README.md` describes the copy-and-`git init` flow, which
  is what the scaffolder does and what P1 exercised.
- A generated repository has no upstream link to its blueprint. Provenance is
  recorded instead: a run records the blueprint `id` and `version` it used, and
  those are in `blueprint.yaml`, which is copied into the generated repository.
  A repository created from a GitHub template does not record its template
  either, so nothing is lost here.
- Updating an existing generated project when its blueprint changes is a
  diff-and-apply, not a merge from an upstream. It was never a merge: a
  template repository has no ongoing relationship with what it created.
- Cost: `slipway-blueprints` grows monotonically, and `npm ci` at its root
  installs the validation tooling for every archetype at once. Both are cheap
  at this size and neither is a one-way door.

## Alternatives considered

- **One repository per archetype, each a real GitHub template.** Rejected for
  the three reasons above. Its one genuine advantage — "Use this template"
  works without a scaffolder — is worth less than it looks, because the
  scaffolder is an agent that has to copy files either way.
- **Directories now, split later, with the README describing the future.**
  Rejected: that is how the README came to describe something that was not
  true. A document describes what is, or it says plainly that it is planned.
