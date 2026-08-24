-- 0007_drop_port_allocations: one mechanism, not two.
--
-- Ports moved onto the deployment record in 0006. A port and the thing holding
-- it were two rows that could disagree; now `deployments.destroyed_at` is both
-- the release and the record of what happened, so they cannot.
--
-- Nothing has written this table since the local container provider replaced
-- compose-over-SSH. Dropping it rather than leaving it empty, because an empty
-- table with a unique index on it reads like a live mechanism to the next
-- person, and they will wire something to it.
--
-- Forward-only, like every migration here. The old rows are not migrated: they
-- described deployments that no longer exist, on a host we no longer deploy to.

DROP TABLE IF EXISTS port_allocations;

COMMENT ON COLUMN deployments.port IS
    'The port this deployment holds. Allocated by inserting this row and losing '
    'to deployments_live_port_key when someone else got there first -- never by '
    'scanning for a port that looks free, because two processes asking that '
    'question at the same moment get the same answer. Released by setting '
    'destroyed_at, which is the same statement that records how the deployment '
    'ended: see docs/decisions/0011-port-allocation-on-the-deployment.md.';
