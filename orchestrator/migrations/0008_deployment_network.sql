-- 0008_deployment_network: what a test runner needs to reach a deployment.
--
-- The published host port is for a human opening a URL. A sibling container on
-- the same Docker network reaches the deployment by container name on its
-- INTERNAL port, which is a different number and was not written down.

ALTER TABLE deployments
    ADD COLUMN container_port integer NOT NULL DEFAULT 3000,
    ADD COLUMN network        text;

COMMENT ON COLUMN deployments.container_port IS
    'The port the application listens on inside the container. What a sibling '
    'container addresses; deployments.port is the host port a browser uses.';

COMMENT ON COLUMN deployments.network IS
    'The user-defined Docker network the container is attached to. Required for '
    'name resolution: containers on the default bridge cannot resolve each '
    'other by name, so a test runner could only reach them through the host.';
