-- 0002_jobs: the worker queue.
--
-- Claimed with FOR UPDATE SKIP LOCKED under a lease. Every job body is
-- idempotent because every job will eventually be retried.

CREATE TABLE jobs (
    id                uuid PRIMARY KEY,
    run_id            uuid        NOT NULL REFERENCES runs (id) ON DELETE RESTRICT,
    kind              text        NOT NULL,
    status            text        NOT NULL DEFAULT 'pending',
    idempotency_key   text        NOT NULL,
    attempts          integer     NOT NULL DEFAULT 0,
    leased_by         text,
    lease_expires_at  timestamptz,
    last_error        text,
    run_after         timestamptz NOT NULL DEFAULT now(),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT jobs_status_known CHECK (status IN (
        'pending', 'leased', 'succeeded', 'failed', 'abandoned'
    )),
    CONSTRAINT jobs_attempts_non_negative CHECK (attempts >= 0),

    -- A leased job has a holder and an expiry; anything else is a lease that
    -- cannot be reclaimed, which is how queues wedge.
    CONSTRAINT jobs_lease_is_complete CHECK (
        (status = 'leased') = (leased_by IS NOT NULL AND lease_expires_at IS NOT NULL)
    )
);

-- Enqueueing the same logical work twice is a no-op, not a duplicate job.
-- This is the constraint that makes retrying an enqueue safe.
CREATE UNIQUE INDEX jobs_idempotency_key_key ON jobs (idempotency_key);

-- The claim query's index: pending or expired work, oldest first.
CREATE INDEX jobs_claimable_idx
    ON jobs (kind, run_after, created_at)
    WHERE status IN ('pending', 'leased');

CREATE INDEX jobs_run_id_idx ON jobs (run_id, created_at DESC);

COMMENT ON TABLE jobs IS
    'Worker queue. Claim with SELECT ... FOR UPDATE SKIP LOCKED and set '
    'lease_expires_at; a dead worker''s jobs become claimable again when the '
    'lease expires, which is why every job body must be idempotent.';

COMMENT ON COLUMN jobs.idempotency_key IS
    'Unique. Derived from (run_id, kind, attempt-defining inputs), so an '
    'enqueue that is retried after a crash collides instead of duplicating.';
