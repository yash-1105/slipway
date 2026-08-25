"""deploy seam: previews as local Docker containers.

Builds the project's own Dockerfile -- not a wrapper around it -- so what runs
in a preview is what the project actually ships. If the Dockerfile is wrong, the
preview is wrong, which is the point: a preview that works when the real build
does not has told you nothing.

Everything that can fail returns a value carrying the log. A build failure is
handed to a human at a gate, or back to an agent to fix; neither can be done
with a traceback.

This is infrastructure. It holds no store: ports are allocated and deployments
recorded by app/services/deploys.py, which calls it with a target that has
already been written down.
"""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import structlog

from app.deploy.base import (
    Deployer,
    DeployFailure,
    Deployment,
    DeploymentTarget,
    DeployResult,
    project_name_for,
)
from app.integrations.process import run as run_process

log = structlog.get_logger(__name__)

#: Every container this provider creates carries these, so reconciliation can
#: find them without a store and a stray container can be traced to a run.
LABEL_MANAGED = "slipway.managed"
LABEL_DEPLOYMENT = "slipway.deployment_id"
LABEL_RUN = "slipway.run_id"

class LocalContainerDeployer(Deployer):
    def __init__(
        self,
        *,
        docker_binary: str,
        public_host: str,
        network: str = "slipway-previews",
        health_timeout_seconds: float = 120.0,
        health_poll_seconds: float = 1.0,
    ) -> None:
        self._docker = docker_binary
        self._public_host = public_host
        self._network = network
        self._health_timeout = health_timeout_seconds
        self._health_poll = health_poll_seconds

    @property
    def network(self) -> str:
        return self._network

    async def ensure_network(self) -> None:
        """Create the preview network if it is not there. Idempotent.

        A user-defined network, not the default bridge: only the former gives
        containers DNS names for each other. Without it a test runner can reach
        a deployment only through the host, which is exactly the code path that
        will not exist on the server.
        """
        exists = await run_process(
            [self._docker, "network", "inspect", self._network], timeout_seconds=30.0
        )
        if exists.ok:
            return
        created = await run_process(
            [self._docker, "network", "create", self._network], timeout_seconds=60.0
        )
        if created.ok:
            log.info("deploy.network_created", network=self._network)
            return
        # Another process created it between the inspect and the create. That is
        # the expected race, not an error.
        if "already exists" not in created.stderr:
            log.warning(
                "deploy.network_create_failed",
                network=self._network,
                stderr=created.stderr.strip()[:300],
            )

    # --- the seam ---------------------------------------------------------

    async def deploy(
        self, target: DeploymentTarget, artifact_uri: str, *, timeout_seconds: float
    ) -> DeployResult:
        image = f"slipway/preview:{target.deployment_id}"

        built = await self._build(target, image, timeout_seconds)
        if built is not None:
            return built

        missing = await self._require_healthcheck(image, target)
        if missing is not None:
            return missing

        await self.ensure_network()

        started = await self._start(target, image, timeout_seconds)
        if isinstance(started, DeployFailure):
            return started

        healthy = await self._await_health(target, started)
        if isinstance(healthy, DeployFailure):
            return healthy

        return healthy

    async def teardown(self, deployment_id: UUID, *, timeout_seconds: float) -> None:
        """Remove the container. Idempotent: removing one that is gone is success."""
        name = container_name_for(deployment_id)
        result = await run_process(
            [self._docker, "rm", "--force", "--volumes", name],
            timeout_seconds=timeout_seconds,
        )
        if result.ok:
            log.info("deploy.torn_down", container=name)
            return

        stderr = result.stderr.strip()
        if "No such container" in stderr or "no such container" in stderr:
            # Already gone. Idempotence means this is the success case, not an
            # error to swallow: a caller retrying a teardown must not fail.
            log.info("deploy.already_torn_down", container=name)
            return

        log.warning("deploy.teardown_failed", container=name, stderr=stderr[:400])

    async def list_live(self) -> list[Deployment]:
        """Every container we manage, running or stopped."""
        result = await run_process(
            [
                self._docker, "ps", "--all",
                "--filter", f"label={LABEL_MANAGED}=true",
                "--format", "{{json .}}",
            ],
            timeout_seconds=60.0,
        )
        if not result.ok:
            log.warning("deploy.list_failed", stderr=result.stderr.strip()[:400])
            return []

        found: list[Deployment] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            labels = _parse_labels(row.get("Labels", ""))
            raw_deployment = labels.get(LABEL_DEPLOYMENT, "")
            raw_run = labels.get(LABEL_RUN, "")
            try:
                deployment_id = UUID(raw_deployment)
                run_id = UUID(raw_run)
            except ValueError:
                # A container wearing our label with an unparseable id is not
                # ours to reconcile. Report it rather than acting on it.
                log.warning("deploy.unparseable_label", container=row.get("Names"))
                continue

            found.append(
                Deployment(
                    deployment_id=deployment_id,
                    run_id=run_id,
                    url="",
                    project_name=str(row.get("Names", "")),
                    artifact_uri="",
                    healthy=str(row.get("State", "")).lower() == "running",
                )
            )
        return found

    # --- steps ------------------------------------------------------------

    async def _build(
        self, target: DeploymentTarget, image: str, timeout_seconds: float
    ) -> DeployFailure | None:
        if not target.context_path:
            return DeployFailure(
                "build_failed",
                "no build context: the target has no context_path, so there is "
                "nothing to build",
                deployment_id=target.deployment_id,
            )

        argv = [self._docker, "build", "--tag", image]
        for key, value in sorted(target.build_args.items()):
            argv += ["--build-arg", f"{key}={value}"]
        argv += ["--label", f"{LABEL_MANAGED}=true", target.context_path]

        log.info(
            "deploy.building",
            deployment_id=str(target.deployment_id),
            build_args=sorted(target.build_args),
            context=target.context_path,
        )
        result = await run_process(argv, timeout_seconds=timeout_seconds)
        if result.ok:
            return None

        # The whole log, not a summary. A human at a gate and an agent asked to
        # fix it both need the compiler's own words.
        combined = (result.stdout + result.stderr).strip()
        if result.timed_out:
            return DeployFailure(
                "timeout",
                f"the image build exceeded {timeout_seconds}s",
                log=combined,
                deployment_id=target.deployment_id,
            )
        return DeployFailure(
            "build_failed",
            f"docker build exited {result.exit_code}",
            log=combined,
            deployment_id=target.deployment_id,
        )

    async def _require_healthcheck(
        self, image: str, target: DeploymentTarget
    ) -> DeployFailure | None:
        """A built image with no HEALTHCHECK is a failed deploy, not a silent one.

        Without one, Docker reports the container healthy the moment it starts,
        and a container that starts and then serves errors is indistinguishable
        from one that works. The failure names the fix, because the person
        reading it is being told their Dockerfile is incomplete.
        """
        result = await run_process(
            [self._docker, "image", "inspect", image, "--format", "{{json .Config.Healthcheck}}"],
            timeout_seconds=60.0,
        )
        declared = result.stdout.strip()
        if result.ok and declared and declared != "null":
            return None

        return DeployFailure(
            "no_healthcheck",
            (
                "the image declares no HEALTHCHECK, so there is no way to know "
                "whether it is serving. Add one to the Dockerfile, for example:\n"
                "  HEALTHCHECK --interval=30s --timeout=5s --start-period=20s "
                "--retries=3 \\\n"
                f"    CMD wget --quiet --tries=1 --spider "
                f"http://127.0.0.1:{target.container_port}/api/health || exit 1"
            ),
            deployment_id=target.deployment_id,
        )

    async def _start(
        self, target: DeploymentTarget, image: str, timeout_seconds: float
    ) -> Deployment | DeployFailure:
        name = container_name_for(target.deployment_id)

        # Remove any container left by a previous attempt at this same
        # deployment. The name is deterministic, so a retry addresses the same
        # container rather than colliding with it.
        await run_process(
            [self._docker, "rm", "--force", name], timeout_seconds=60.0
        )

        argv = [
            self._docker, "run", "--detach",
            "--name", name,
            "--label", f"{LABEL_MANAGED}=true",
            "--label", f"{LABEL_DEPLOYMENT}={target.deployment_id}",
            "--label", f"{LABEL_RUN}={target.run_id}",
            # Loopback only. A preview is for us and the client we send the URL
            # to over a tunnel, not for the internet.
            # Both: the network gives sibling containers a name to reach, the
            # publish gives a human a URL to open.
            "--network", self._network,
            "--network-alias", name,
            "--publish", f"127.0.0.1:{target.port}:{target.container_port}",
            "--restart", "unless-stopped",
        ]
        for key, value in sorted(target.runtime_env.items()):
            argv += ["--env", f"{key}={value}"]
        argv.append(image)

        result = await run_process(argv, timeout_seconds=timeout_seconds)
        if not result.ok:
            return DeployFailure(
                "start_failed",
                result.stderr.strip() or f"docker run exited {result.exit_code}",
                log=(result.stdout + result.stderr).strip(),
                deployment_id=target.deployment_id,
            )

        return Deployment(
            deployment_id=target.deployment_id,
            run_id=target.run_id,
            url=f"http://{self._public_host}:{target.port}",
            project_name=name,
            artifact_uri=result.stdout.strip()[:64],  # the container id
            healthy=False,
        )

    async def _await_health(
        self, target: DeploymentTarget, started: Deployment
    ) -> Deployment | DeployFailure:
        """Poll the container's own HEALTHCHECK until it passes, or give up.

        Bounded, because a container that never becomes healthy would otherwise
        hold a worker for as long as it kept failing.
        """
        name = started.project_name
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._health_timeout

        while loop.time() < deadline:
            result = await run_process(
                [
                    self._docker, "inspect", "--format",
                    "{{.State.Status}} "
                    "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                    name,
                ],
                timeout_seconds=30.0,
            )
            if not result.ok:
                return DeployFailure(
                    "start_failed",
                    f"the container disappeared while waiting for it: {result.stderr.strip()}",
                    deployment_id=target.deployment_id,
                )

            state, _, health = result.stdout.strip().partition(" ")
            if health == "healthy":
                log.info("deploy.healthy", container=name, url=started.url)
                return Deployment(
                    deployment_id=started.deployment_id,
                    run_id=started.run_id,
                    url=started.url,
                    project_name=started.project_name,
                    artifact_uri=started.artifact_uri,
                    healthy=True,
                )

            if state == "exited":
                logs = await run_process(
                    [self._docker, "logs", "--tail", "200", name], timeout_seconds=30.0
                )
                return DeployFailure(
                    "unhealthy",
                    "the container exited before becoming healthy",
                    log=(logs.stdout + logs.stderr).strip(),
                    deployment_id=target.deployment_id,
                )

            await asyncio.sleep(self._health_poll)

        logs = await run_process(
            [self._docker, "logs", "--tail", "200", name], timeout_seconds=30.0
        )
        return DeployFailure(
            "unhealthy",
            f"the container did not become healthy within {self._health_timeout}s",
            log=(logs.stdout + logs.stderr).strip(),
            deployment_id=target.deployment_id,
        )


def container_name_for(deployment_id: UUID) -> str:
    """The container's name. One-to-one with the deployment's project name."""
    return project_name_for(deployment_id)


def _parse_labels(raw: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for pair in raw.split(","):
        key, _, value = pair.partition("=")
        if key:
            labels[key.strip()] = value.strip()
    return labels


__all__ = [
    "LABEL_DEPLOYMENT",
    "LABEL_MANAGED",
    "LABEL_RUN",
    "LocalContainerDeployer",
    "container_name_for",
]
