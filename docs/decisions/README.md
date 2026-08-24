# Decisions

One file per decision, numbered, never renumbered. A decision that turns out to
be wrong gets a new ADR that supersedes it; the old one stays, marked
Superseded, because the reasoning is the useful part even when the conclusion
was not.

| | Decision | Status |
| --- | --- | --- |
| [0001](0001-seams.md) | Five seams behind Protocols | Accepted |
| [0002](0002-layering.md) | Layering, and the direction of dependency | Accepted |
| [0003](0003-uuidv7-identifiers.md) | UUIDv7 primary keys, generated in-process | Accepted |
| [0004](0004-novita-base-url.md) | Novita base URL and API surface | **Proposed** — blocked on a live call |
| 0005 | _unclaimed_ | — |
| [0006](0006-scratch-org.md) | Generated repositories live in a scratch org | Accepted |
| [0007](0007-prompts-and-evals-stay-in-slipway.md) | Prompts and evals stay in this repository | Accepted |
| [0008](0008-blueprints-stay-directories.md) | Blueprints stay directories in one repository | Accepted |

0006 and 0007 were written to match references that already existed in
`slipway-blueprints/README.md`. 0005 is unclaimed: if it was meant for something
in that original numbering, it is still free.

## When an ADR is required

CLAUDE.md: a dependency that is structural, and anything that would change one
of the five seams or the layering. Adding a sixth seam requires one. So does
adding anything from the V2 list.
