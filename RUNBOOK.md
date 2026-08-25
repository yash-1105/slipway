# Slipway runbook

Operational procedures for Version 1. Each section: how you notice, how you
confirm, what you do, what you check afterwards.

Every procedure assumes the `SLIPWAY_` environment (see `deploy/.env.example`)
is exported and you are running `slipway` — the CLI in `orchestrator/app/cli/` —
from a checkout.

**Every command prescribed here exists.** Where a procedure needs something the
CLI does not have yet, it says so and gives what actually works today instead.
The gaps are listed together at the end under *Not built yet*, so you find out
before an incident rather than during one.
`scripts/check_runbook_commands.py`, run by `make check`, fails if this document
tells you to run something that is not there.

Some steps use `psql`. `SLIPWAY_DATABASE_URL` is a SQLAlchemy URL and psql will
not take it; drop the driver:

    DB="$(printf '%s' "$SLIPWAY_DATABASE_URL" | sed 's|+asyncpg||')"

---

## A run is stuck

**Symptom.** A run sits in a non-terminal state and its `updated_at` has not
moved for longer than the job lease.

**Confirm.**

    slipway runs list --stuck
    slipway runs show <run-id>

Then look at the job behind it:

    psql "$DB" -c "
      SELECT id, kind, status, attempts, leased_by, lease_expires_at, left(last_error, 120)
      FROM jobs WHERE run_id = '<run-id>' ORDER BY created_at DESC;"

Which of four it is:

1. **`leased`, lease in the future.** A worker is on it. Leave it alone.
2. **`leased`, lease expired.** No worker has reclaimed it — see *A worker is
   dead*. Any live worker picks it up within one poll interval.
3. **`pending`.** Queued, and no worker is claiming its kind. Check
   `SLIPWAY_WORKER_KINDS` on the running workers.
4. **`abandoned`, or no job at all, while the run is non-terminal.** This is the
   real stuck case. `abandoned` means attempts were exhausted; the worker should
   already have failed the run, and if it did not, that is a bug. Capture the
   events before touching anything:

       slipway runs show <run-id>

**Act.** There is no unstick command. For case 4 the honest options are:

- **Cancel and start again.** The supported action, and the right one when the
  run has not passed a gate:

      slipway runs cancel <run-id> <your-name> "stuck in <state>, see events"

- **Re-queue the job by hand.** A manual intervention. Only do this once you
  have read the events and understand why the job died, because you are
  overriding the attempt cap that stopped it:

      psql "$DB" -c "
        UPDATE jobs SET status = 'pending', attempts = 0, leased_by = NULL,
                        lease_expires_at = NULL, run_after = now()
        WHERE id = '<job-id>' AND status = 'abandoned';"

  Every job body is idempotent, so re-running one is safe. What is not safe is
  doing this repeatedly to a job failing for the same reason each time.

**Afterwards.** The run should move within one poll interval. If it sticks again
in the same state, stop and read the events. Do not loop on the re-queue.

---

## A worker is dead

**Symptom.** Jobs are leased but not progressing.

**Confirm.** There is no command that lists workers. Ask the database who holds
leases and when they expire:

    psql "$DB" -c "
      SELECT leased_by, count(*), min(lease_expires_at) AS soonest_expiry
      FROM jobs WHERE status = 'leased' GROUP BY leased_by ORDER BY soonest_expiry;"

A `leased_by` whose `soonest_expiry` is in the past holds nothing real. Worker
ids are `hostname:pid` unless `SLIPWAY_WORKER_ID` is set, so the row tells you
which machine to look at.

    docker compose --file deploy/compose.yaml ps

**Act.** Expired leases are reclaimed automatically by any live worker. Start
one and wait a full `SLIPWAY_JOB_LEASE_SECONDS` before intervening further.

    docker compose --file deploy/compose.yaml up --detach worker

If the process is alive but wedged, restart it. Do not delete job rows: a
half-finished job is recovered by its lease expiring, not by deletion.

**Afterwards.** Re-run the query. The dead worker's leases should have moved to
a live worker id, and the leased count should be falling.

---

## Docker is out of disk

**Slipway needs at least 30 GB of Docker disk.** Measured: 23.8 GB free after a
full test suite on a 31.4 GB disk. Docker Desktop's default is far less. Raise
it in Settings, Resources, Disk image size — the daemon restarts, stops every
running container, and **recreates the disk, which removes every image**, so do
it between runs and expect to re-pull.

What consumes it: the Playwright runner image is 3.8 GB, each preview image is
roughly 90–300 MB, Postgres is 400 MB, and a single Next.js build can leave 2.8 GB
of build cache.

**The symptom does not point at the disk.** All of these were observed within an
hour of each other on a 7.8 GB disk, and each one sends you somewhere else:

| What you see | What it actually is |
| --- | --- |
| `docker pull` reports success, image is absent | The pull failed partway; piped through `tail`, the pipeline's exit code was `tail`'s |
| Tests skip for a "missing" image | The pull above never completed |
| A build fails on an unrelated compile error | It got far enough to fail differently; look above the error |
| Postgres exits and comes back in recovery mode | Killed mid-write. `PANIC: could not write to file "pg_logical/replorigin_checkpoint.tmp": No space left on device` |
| Postgres will not leave recovery | It cannot checkpoint, because there is still no space |

**Confirm.** Ask the daemon, not the host — on macOS the Docker VM has its own
disk and `df` on the Mac tells you nothing:

    docker run --rm alpine:3.20 df -h /
    docker system df

**Act**, in increasing order of destructiveness. Build cache is what grows:
a single Next.js build left 2.8 GB of it, and nothing prunes it on its own.

    docker system df                  # where it went; check Build Cache first
    docker builder prune -af          # regenerable, safe, usually enough
    docker system df                  # confirm it came back

    slipway reconcile --dry-run       # orphaned preview images and containers
    slipway reconcile --apply

`docker system df -v` lists images individually when the summary is not enough.

Reconcile knows about preview images: teardown removes a deployment's image, and
anything left behind is a crash between building and tearing down. It will not
touch an image a live deployment is using.

If that is not enough, raise the disk size. Do not `docker system prune -a`: it
removes images this machine did not build and cannot cheaply replace.

**Afterwards.** If Postgres was killed, check it actually recovered before
trusting it:

    docker exec <postgres-container> pg_isready
    psql "$DB" -c "SELECT count(*) FROM deployments;"

---

## Orphan cleanup

**Symptom.** Containers or held ports exist that no live run owns — usually
after a crash between recording an identifier and finishing the operation, or a
container removed by hand.

Reconciliation repairs both directions: a container nothing claims is removed,
and a record whose container has vanished is settled and its port released.

**Confirm.** The reconciler reports and does not act:

    slipway reconcile --dry-run

Read the list. A successfully deployed run keeps its deployment and its port —
those are not orphans, and the reconciler knows it. Anything listed that belongs
to a run you care about is a bug in the reconciler, not permission to proceed.

**Act.**

    slipway reconcile --apply

**Afterwards.** A second `--dry-run` should print `clean`. Ports are released by
updating the allocation row; never free one by killing whatever is listening.

> `reconcile --apply` has no test covering it; `--dry-run` does. Read the
> dry-run output carefully before applying.

---

## Provider outage (Novita)

**Symptom.** Model calls fail with 5xx or time out; runs pile up in an agent
state.

**Confirm.**

    slipway models ping
    slipway models catalogue

`ping` makes one real call through the models seam. `catalogue` shows which
endpoint the model ids came from and when they were synced.

**Act.** Retries are capped with backoff, so runs fail rather than hang. There
is no pause command. To stop new work being picked up, stop the worker — the
API keeps accepting briefs and they queue:

    docker compose --file deploy/compose.yaml stop worker

When the provider recovers:

    docker compose --file deploy/compose.yaml start worker

Queued jobs are claimed in order. Runs that already failed stay failed; a failed
run is not resumed, it is re-created from its brief.

**Afterwards.** If the outage changed the API surface — a base URL, a retired
model id — regenerate the catalogue and record what changed in an ADR:

    make models-sync

Never hand-edit `config/models.yaml`.

---

## Budget exhausted

**Read this first: budgets are not enforced.** `SLIPWAY_BUDGET_DAILY_USD_CAP`
and `SLIPWAY_BUDGET_RUN_USD_CAP` are validated at startup and read by nothing
else. There is no spend tracking and no cap that stops a run. Setting them
changes nothing.

**Symptom.** Spend is higher than expected, or Novita starts refusing calls.

**Confirm.** Novita's own dashboard is the only source of spend today. Within
Slipway you can see token usage per completed agent step, because the worker
logs it:

    docker compose --file deploy/compose.yaml logs worker | grep agent.node_completed

**Act.** Stop the worker. That is the whole of the control we have:

    docker compose --file deploy/compose.yaml stop worker

Then read the events of the runs that were in flight. The usual cause of a
disproportionate spend is a graph looping on a failing step, which no cap would
have fixed — it would only have hidden it.

**Afterwards.** Nothing here is automatic. Until budgets are enforced, treat
this section as "how to stop the bleeding", not as a control.

---

## Rollback

**Read this first: rollback is not implemented.** The `deployments` table exists
but nothing writes to it, so there is no recorded history to roll back *to*, and
`ComposeOverSshDeployer.rollback` returns a failure value saying the deployer
keeps no history.

**What you can actually do.** Previews are local Docker containers named
`slipway-preview-<deployment-id>`:

    docker ps --all --filter label=slipway.managed=true

The record behind one, including the port it holds:

    psql "$DB" -c "
      SELECT id, run_id, status, port, container_name, url, destroyed_at
      FROM deployments WHERE destroyed_at IS NULL ORDER BY created_at DESC;"

To take a bad preview down, remove the container and let the reconciler settle
the record and release the port:

    docker rm --force slipway-preview-<deployment-id>
    slipway reconcile --dry-run
    slipway reconcile --apply

Do not delete the `deployments` row by hand. `destroyed_at` is what releases the
port, and the reconciler sets it as part of recording what happened.

There is no supported way to put a previous version back. If a preview is bad,
take it down, or run the pipeline again from the brief.

There is no supported way to put a previous version back. If a deployed
application is bad, the honest options are to take it down, or to run the
pipeline again from the brief.

---

## Rolling back the orchestrator itself

Migrations are forward-only. There is no down-migration and there will not be
one. To roll back the orchestrator, deploy the previous image *only if* it is
compatible with the current schema; if it is not, roll forward with a new
migration instead. `orchestrator/migrations/` and the `schema_migrations` table
tell you what has been applied:

    psql "$DB" -c "SELECT version, applied_at FROM schema_migrations ORDER BY version;"

---

## Not built yet

Commands referenced in conversation and in earlier drafts of this runbook.
**None of them exists.** They are listed so that finding one missing is not a
surprise, and so nobody writes a procedure around one by accident.

| Command | What it would do | Procedure above uses instead |
| --- | --- | --- |
| `runs unstick` | Re-enqueue from the last good state | Manual SQL, or cancel |
| `workers list` | Heartbeat age per worker | SQL over the `jobs` table |
| `runs pause` / `runs resume` | Stop and resume claiming work | Stop and start the worker container |
| `budget show` | Spend per run and per day against a cap | Nothing — budgets are not enforced at all |
| `deploys list` | Deployment history for a run | Nothing — the table is never written |
| `deploys rollback` | Re-deploy a recorded artifact | Nothing — take the deployment down instead |

Adding any of these means adding the command, its service and its test in the
same change, and deleting its row from this table.
