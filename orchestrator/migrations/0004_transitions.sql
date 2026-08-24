-- 0004_transitions: the state transition table, in the database.
--
-- app/state/machine.py is the source of truth for the application; this table
-- is the same table, so that a transition can be checked in SQL and so that a
-- migration adding a state is forced to say what moves in and out of it.
-- tests/integration/test_state_table_matches_db.py fails if the two drift.

CREATE TABLE run_transitions (
    source   text NOT NULL,
    trigger  text NOT NULL,
    target   text NOT NULL,
    gate     text,

    PRIMARY KEY (source, trigger),
    CONSTRAINT run_transitions_gate_known CHECK (gate IS NULL OR gate IN ('spec', 'deploy'))
);

INSERT INTO run_transitions (source, trigger, target, gate) VALUES
    ('created',       'start',            'specifying',    NULL),
    ('specifying',    'agent_succeeded',  'spec_review',   'spec'),
    ('specifying',    'agent_failed',     'failed',        NULL),
    ('spec_review',   'approved',         'building',      NULL),
    ('spec_review',   'rejected',         'specifying',    NULL),
    ('building',      'agent_succeeded',  'testing',       NULL),
    ('building',      'agent_failed',     'failed',        NULL),
    ('testing',       'agent_succeeded',  'deploy_review', 'deploy'),
    ('testing',       'agent_failed',     'failed',        NULL),
    ('deploy_review', 'approved',         'deploying',     NULL),
    ('deploy_review', 'rejected',         'building',      NULL),
    ('deploying',     'deploy_succeeded', 'deployed',      NULL),
    ('deploying',     'deploy_failed',    'failed',        NULL),
    -- Cancellation is legal from every non-terminal state.
    ('created',       'cancelled',        'cancelled',     NULL),
    ('specifying',    'cancelled',        'cancelled',     NULL),
    ('spec_review',   'cancelled',        'cancelled',     NULL),
    ('building',      'cancelled',        'cancelled',     NULL),
    ('testing',       'cancelled',        'cancelled',     NULL),
    ('deploy_review', 'cancelled',        'cancelled',     NULL),
    ('deploying',     'cancelled',        'cancelled',     NULL);

COMMENT ON TABLE run_transitions IS
    'Mirror of app/state/machine.py. Adding a run state means adding rows here '
    'in the same migration that widens the runs_state_known CHECK constraint.';
