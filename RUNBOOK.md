# Slipway runbook

Operational procedures for Version 1. Each section: how you notice, how you
confirm, what you do, what you check afterwards.

Every procedure below assumes `SLIPWAY_DATABASE_URL` is exported and you are
running `slipway` (the CLI, `orchestrator/app/cli/`) from a checkout. Nothing
here requires editing the database by hand except where it says so explicitly.

---

## A run is stuck

**Symptom.** A run sits in a non-terminal state and its `updated_at` has not
moved for longer than the job lease.

**Confirm.**

    slipway runs list --stuck
    slipway runs show <run-id>          # last events, current job, lease holder

Decide which of three it is: (a) the job is still leased and a worker is alive
and working — leave it; (b) the lease has expired and no worker re-claimed it —
see *A worker is dead*; (c) the job completed but the run never transitioned —
a bug; capture the events and file it before touching anything.

**Act.**

    slipway runs unstick <run-id>       # re-enqueues from the last good state

**Afterwards.** The run should re-enter the state machine within one poll
interval. If it sticks again in the same state, stop and read the events; do
not loop on `unstick`.

---

## A worker is dead

**Symptom.** Jobs are leased but not progressing; no worker heartbeat.

**Confirm.**

    slipway workers list                # heartbeat age per worker
    docker compose -f deploy/compose.yaml ps

**Act.** Expired leases are reclaimed automatically by any live worker — start
one and wait one lease period before intervening further.

    docker compose -f deploy/compose.yaml up -d worker

If the process is alive but wedged, restart it. Do not delete job rows; a
half-finished job is recovered by its idempotency key, not by deletion.

**Afterwards.** Confirm `slipway workers list` shows a fresh heartbeat and the
leased-job count is falling.

---

## Orphan cleanup

**Symptom.** Containers, compose projects, allocated ports or sandbox
workspaces exist that no live run owns — usually after a crash between
recording an external identifier and finishing the operation.

**Confirm.** The reconciler reports, and does not act, in dry-run:

    slipway reconcile --dry-run

Read the list. Anything it wants to delete that belongs to a run you care about
is a bug in the reconciler, not permission to proceed.

**Act.**

    slipway reconcile --apply

**Afterwards.** A second `--dry-run` should be empty. Port allocations are
released by deleting the allocation row; never free a port by killing whatever
is listening on it.

---

## Provider outage (Novita)

**Symptom.** Model calls fail with 5xx or time out; runs pile up in an agent
state.

**Confirm.**

    slipway models ping                 # one real call per configured role

**Act.** Retries with backoff are already capped, so runs will fail rather than
hang. Pause intake so the failures stop accumulating:

    slipway runs pause --all

When the provider recovers, `slipway runs resume --all`. Failed runs resume
from their last committed state; agent work already paid for is not repeated.

**Afterwards.** If the outage changed anything about the API surface — a base
URL, a removed model id — regenerate `config/models.yaml` from the live
endpoint and record what changed in an ADR. Do not hand-edit the file.

---

## Budget exhausted

**Symptom.** Runs fail at an agent step with a budget error, or the daily spend
cap is hit.

**Confirm.**

    slipway budget show                 # spend per run, per day, against cap

**Act.** Raise the cap deliberately, in configuration, or let the runs fail.
Do not disable the check to get a run through — a run that costs more than the
cap is information, and the cap exists because two people share one key.

**Afterwards.** If one run consumed a disproportionate share, read its events
before raising anything; the usual cause is a graph looping on a failing tool
call, which a higher cap will not fix.

---

## Rollback

**Symptom.** A deploy is bad and the previous one was good.

**Confirm.** Every deploy records a deployment id and the artifact URI it came
from:

    slipway deploys list <run-id>

**Act.**

    slipway deploys rollback <run-id> --to <deployment-id>

This re-deploys a recorded artifact. It does not rebuild, and it does not
re-run the agents — the artifact is content-hashed and immutable.

**Afterwards.** Confirm the URL serves the expected build, then reconcile:
the superseded deployment's containers and port allocation are released by
`slipway reconcile --apply`, not by hand.

---

## Rolling back the orchestrator itself

Migrations are forward-only. There is no down-migration and there will not be
one. To roll back the orchestrator, deploy the previous image *only if* it is
compatible with the current schema; if it is not, roll forward with a new
migration instead. Check `orchestrator/migrations/` for what has been applied
since the image you are considering.
