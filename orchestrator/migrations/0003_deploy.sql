-- 0003_deploy: artifacts, deployments, and the port allocator.

CREATE TABLE artifacts (
    id          uuid PRIMARY KEY,
    run_id      uuid        NOT NULL REFERENCES runs (id) ON DELETE RESTRICT,
    kind        text        NOT NULL,
    uri         text        NOT NULL,
    sha256      char(64)    NOT NULL,
    size_bytes  bigint      NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT artifacts_size_non_negative CHECK (size_bytes >= 0),
    CONSTRAINT artifacts_sha256_is_hex CHECK (sha256 ~ '^[0-9a-f]{64}$'),

    -- URIs carry an explicit scheme so that file:// rows stay resolvable after
    -- the artifacts seam moves to s3:// in V2.
    CONSTRAINT artifacts_uri_has_scheme CHECK (uri ~ '^[a-z][a-z0-9+.-]*://')
);

CREATE UNIQUE INDEX artifacts_run_kind_sha_key ON artifacts (run_id, kind, sha256);
CREATE INDEX artifacts_run_id_idx ON artifacts (run_id, created_at DESC);

COMMENT ON TABLE artifacts IS
    'Content-addressed and immutable. Storing the same bytes twice for one '
    '(run, kind) collides on the unique index rather than creating a second row.';


CREATE TABLE deployments (
    id            uuid PRIMARY KEY,
    run_id        uuid        NOT NULL REFERENCES runs (id) ON DELETE RESTRICT,
    artifact_uri  text        NOT NULL,
    project_name  text        NOT NULL,
    host          text        NOT NULL,
    port          integer     NOT NULL,
    url           text,
    status        text        NOT NULL DEFAULT 'recording',
    log           text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    settled_at    timestamptz,

    CONSTRAINT deployments_status_known CHECK (status IN (
        'recording', 'live', 'failed', 'superseded', 'torn_down'
    )),
    CONSTRAINT deployments_port_range CHECK (port BETWEEN 1 AND 65535)
);

CREATE UNIQUE INDEX deployments_project_name_key ON deployments (project_name);
CREATE INDEX deployments_run_id_idx ON deployments (run_id, created_at DESC);

COMMENT ON COLUMN deployments.status IS
    '''recording'' is written BEFORE the compose project is created, so a crash '
    'mid-deploy leaves a row the reconciler can find. A deployment that exists '
    'on the server with no row here is the one case we cannot recover.';


CREATE TABLE port_allocations (
    id            uuid PRIMARY KEY,
    run_id        uuid        NOT NULL REFERENCES runs (id) ON DELETE RESTRICT,
    host          text        NOT NULL,
    port          integer     NOT NULL,
    allocated_at  timestamptz NOT NULL DEFAULT now(),
    released_at   timestamptz,

    CONSTRAINT port_allocations_port_range CHECK (port BETWEEN 1 AND 65535)
);

-- The whole point of this table: the database decides who gets a port, by
-- rejecting the second inserter. Nothing scans for a port that looks free.
CREATE UNIQUE INDEX port_allocations_live_key
    ON port_allocations (host, port)
    WHERE released_at IS NULL;

COMMENT ON TABLE port_allocations IS
    'Shared-resource allocation by unique constraint. A caller inserts the port '
    'it wants and finds out from the constraint whether it won. Releasing sets '
    'released_at; the partial unique index then frees the port for reuse while '
    'keeping the history.';
