-- 0006_local_deployments: previews as local containers.
--
-- Adds what a container deployment needs to be recoverable: the name we will
-- give the container (known before it exists), the id Docker assigns, and the
-- moment it was destroyed.
--
-- The unique index is the point. Two workers deploying at the same moment
-- cannot pick the same port, because the second insert loses. Nothing scans
-- for a free port; scanning is how two processes agree on the same answer.

ALTER TABLE deployments
    ADD COLUMN container_name text,
    ADD COLUMN container_id   text,
    ADD COLUMN destroyed_at   timestamptz,
    ADD COLUMN image_tag      text;

-- One live deployment per port. A row is live until it is destroyed, so a
-- failed deploy must set destroyed_at to give its port back -- which makes
-- releasing the port the same operation as recording the failure, rather than
-- a second step someone can forget.
CREATE UNIQUE INDEX deployments_live_port_key
    ON deployments (port)
    WHERE destroyed_at IS NULL;

-- Reconciliation looks up a record by the container it can see.
CREATE UNIQUE INDEX deployments_container_name_key
    ON deployments (container_name)
    WHERE container_name IS NOT NULL AND destroyed_at IS NULL;

COMMENT ON COLUMN deployments.container_name IS
    'Deterministic, derived from the deployment id, and written BEFORE the '
    'container is created. A crash between the insert and `docker run` leaves '
    'a row naming something that does not exist, which the reconciler can '
    'repair. The reverse -- a container with no row -- is the case that is not '
    'recoverable, which is why the row goes first.';

COMMENT ON COLUMN deployments.destroyed_at IS
    'NULL means this deployment still holds its port. Set it to release the '
    'port; the partial unique index above does the rest.';

COMMENT ON INDEX deployments_live_port_key IS
    'Port allocation. Callers insert the port they want and find out from this '
    'index whether they won. Losing is a normal outcome: try the next port.';
