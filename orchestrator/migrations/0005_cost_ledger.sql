-- 0005_cost_ledger: what every model call cost, and who it was for.
--
-- Written in the same transaction as the job outcome it belongs to, so a run
-- cannot end up with an outcome and no cost, or a cost and no outcome.

CREATE TABLE cost_entries (
    id                 uuid PRIMARY KEY,
    run_id             uuid        NOT NULL REFERENCES runs (id) ON DELETE RESTRICT,
    job_id             uuid        REFERENCES jobs (id) ON DELETE RESTRICT,

    role               text        NOT NULL,

    -- Both, deliberately. A provider that serves something other than what was
    -- asked for is a thing we need to be able to see afterwards, and a ledger
    -- that records only the intention cannot show it.
    model_requested    text        NOT NULL,
    model_used         text        NOT NULL,

    prompt_tokens      integer     NOT NULL,
    completion_tokens  integer     NOT NULL,

    -- numeric, never float. A cent that rounds differently on two machines is a
    -- reconciliation nobody can close.
    usd                numeric(18, 8) NOT NULL,
    inr                numeric(18, 6) NOT NULL,

    -- The rate this row was converted at, stored per row. The rate moves; a
    -- ledger that stores only the INR is unreadable a month later, and one that
    -- recomputes from today's rate is wrong about what was spent.
    usd_to_inr         numeric(12, 6) NOT NULL,

    -- False when the provider served a model we hold no published price for, so
    -- the amounts are an estimate. A zero meaning "free" and a zero meaning
    -- "unknown" are different numbers.
    priced             boolean     NOT NULL DEFAULT true,

    actor              text        NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT cost_entries_tokens_non_negative
        CHECK (prompt_tokens >= 0 AND completion_tokens >= 0),
    CONSTRAINT cost_entries_amounts_non_negative
        CHECK (usd >= 0 AND inr >= 0),
    CONSTRAINT cost_entries_rate_positive
        CHECK (usd_to_inr > 0),
    CONSTRAINT cost_entries_actor_not_blank
        CHECK (length(btrim(actor)) > 0),
    CONSTRAINT cost_entries_models_not_blank
        CHECK (length(btrim(model_requested)) > 0 AND length(btrim(model_used)) > 0)
);

CREATE INDEX cost_entries_run_id_idx ON cost_entries (run_id, id);
CREATE INDEX cost_entries_created_at_idx ON cost_entries (created_at DESC);

COMMENT ON TABLE cost_entries IS
    'One row per model call. Written in the same transaction as the job outcome '
    'it belongs to; see app/services/jobs.py. Budgets are not enforced against '
    'this yet -- see docs/notes/observations.md.';

COMMENT ON COLUMN cost_entries.model_used IS
    'What the provider says it served, from the completion response, not what '
    'was requested. These differ when a fallback fires or the provider '
    'substitutes.';

COMMENT ON COLUMN cost_entries.usd_to_inr IS
    'The rate applied to this row, at the time it was written. Stored per row '
    'because the rate moves and a historical total must not silently change.';
