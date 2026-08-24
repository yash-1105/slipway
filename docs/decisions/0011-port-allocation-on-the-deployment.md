# 0011 — Port allocation lives on the deployment record

**Status:** Accepted
**Date:** 2026-08-25
**Supersedes:** the `port_allocations` table from migration 0003, dropped in 0007

## Context

Migration 0003 gave ports their own table. A `port_allocations` row held
`(host, port, run_id)` with a partial unique index on `(host, port) WHERE
released_at IS NULL`, and a deployment was a separate row in `deployments`.

The allocation mechanism itself was right, and is unchanged: claim a port by
inserting and losing to a unique index, never by scanning for one that looks
free, because two processes asking that question at the same moment get the
same answer and then both try to bind it.

What was wrong was that it was a *second row*.

## Decision

A port is a column on the deployment that holds it. `deployments.port`, with a
partial unique index on `(port) WHERE destroyed_at IS NULL`.

`port_allocations` is dropped.

**A port and the thing holding it were two rows that could disagree.** Every
operation had to keep them in step, and each was a place they could come apart:

- A deploy that failed after allocating had to release the port *and* record the
  failure. Two statements, and forgetting the first leaked a port out of a
  bounded range.
- A deployment torn down had to release its port separately.
- Reconciliation had to check both — a live allocation whose deployment was gone,
  and a live deployment whose allocation was gone — and repair each.

With the port on the record, `destroyed_at` is both the release and the record
of how the deployment ended. Releasing the port is the same statement as
recording the outcome, so a deployment cannot end up settled while still holding
a port, or holding a port with no record of why.

That is the whole argument. The rest is consequence.

## Consequences

- `DeploymentRepository.settle(..., release_port=True)` is the only way a port
  comes back. There is no separate release call to forget.
- Reconciliation has one question to ask instead of two: does a record holding a
  port still have a container, and does a container still have a record. The
  `stale_port_allocations` branch is gone; `vanished_deployments` covers it, and
  `apply()` settles orphaned and vanished records in the same pass that removes
  their containers.
- One fewer table, one fewer repository, one fewer service. `services/ports.py`
  and its tests are deleted.
- The old table's rows are not migrated. They described deployments that no
  longer exist, on a host we no longer deploy to.
- Cost: a deployment row is now load-bearing for two things — what was deployed,
  and which port it holds. If a future backend allocates something else scarce
  (a subdomain, a database), the same argument says it belongs on this row too,
  and the row gets wider. That is the trade, and it is the right one at this
  size.

## Why not keep the table and stop writing it

Considered, and rejected. An empty table with a unique index on it reads like a
live mechanism to the next person, and they will wire something to it — which
is precisely the two-mechanism state this ADR exists to end. The reconciler was
already reading it: a live code path querying a table nothing populated, which
could never fire and which nobody would notice was dead.

## What would reopen this

A deploy backend where the port outlives the deployment — a reserved pool
assigned ahead of time, or ports leased across deployments of the same run. Then
the port genuinely is a separate lifetime and deserves a separate row again.
