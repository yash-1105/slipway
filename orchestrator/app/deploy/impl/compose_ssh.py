"""deploy seam: `docker compose` over SSH to the one V1 server.

One compose project per run, named from the deployment id so a retry addresses
the same project. The port comes from the database allocator; nothing here
scans for a free one.
"""

from __future__ import annotations

import shlex
from uuid import UUID

import structlog

from app.deploy.base import (
    Deployer,
    DeployFailure,
    Deployment,
    DeploymentTarget,
    DeployResult,
)
from app.integrations.process import ProcessResult
from app.integrations.process import run as run_process

log = structlog.get_logger(__name__)


class ComposeOverSshDeployer(Deployer):
    def __init__(
        self,
        *,
        ssh_host: str,
        ssh_user: str,
        remote_root: str,
        public_host: str,
        ssh_binary: str,
    ) -> None:
        self._host = ssh_host
        self._user = ssh_user
        self._remote_root = remote_root
        self._public_host = public_host
        self._ssh = ssh_binary

    async def deploy(
        self, target: DeploymentTarget, artifact_uri: str, *, timeout_seconds: float
    ) -> DeployResult:
        project_dir = f"{self._remote_root}/{target.project_name}"
        script = " && ".join(
            [
                f"mkdir -p {shlex.quote(project_dir)}",
                f"cd {shlex.quote(project_dir)}",
                # `up -d` is idempotent: re-running converges rather than
                # stacking a second copy, which is what makes job retry safe.
                f"SLIPWAY_PORT={target.port} docker compose up --detach --wait",
            ]
        )
        result = await self._ssh_run(script, timeout_seconds=timeout_seconds)

        if result.timed_out:
            return DeployFailure(
                "timeout",
                f"compose up did not settle within {timeout_seconds}s",
                log=result.stdout + result.stderr,
                deployment_id=target.deployment_id,
            )
        if not result.ok:
            # `--wait` exits non-zero when a container never becomes healthy,
            # which is the common case and is why the log travels with the
            # failure instead of being raised away.
            return DeployFailure(
                "unhealthy" if "unhealthy" in result.stderr else "start_failed",
                result.stderr.strip() or f"compose up exited {result.exit_code}",
                log=result.stdout + result.stderr,
                deployment_id=target.deployment_id,
            )

        return Deployment(
            deployment_id=target.deployment_id,
            run_id=target.run_id,
            url=f"http://{self._public_host}:{target.port}",
            project_name=target.project_name,
            artifact_uri=artifact_uri,
            healthy=True,
        )

    async def rollback(
        self, run_id: UUID, to_deployment_id: UUID, *, timeout_seconds: float
    ) -> DeployResult:
        # Rollback re-deploys a recorded artifact; the caller reads the
        # DeploymentTarget for `to_deployment_id` out of the database and hands
        # it back through `deploy`. Nothing is rebuilt here.
        return DeployFailure(
            "unreachable",
            "rollback is driven by services.deploys.rollback, which reloads the "
            "recorded target and calls deploy(); the deployer itself keeps no history",
            deployment_id=to_deployment_id,
        )

    async def teardown(self, deployment_id: UUID, *, timeout_seconds: float) -> None:
        project = _project_name(deployment_id)
        script = (
            f"cd {shlex.quote(f'{self._remote_root}/{project}')} 2>/dev/null "
            f"&& docker compose down --volumes --remove-orphans || true"
        )
        await self._ssh_run(script, timeout_seconds=timeout_seconds)

    async def list_live(self) -> list[Deployment]:
        result = await self._ssh_run(
            "docker compose ls --format json --all", timeout_seconds=60.0
        )
        if not result.ok:
            log.warning("deploy.list_live_failed", stderr=result.stderr.strip())
            return []
        # Parsed by the reconciler, which owns the mapping from compose project
        # back to a run; this seam reports names, not ownership.
        return _parse_compose_ls(result.stdout)

    async def _ssh_run(self, script: str, *, timeout_seconds: float) -> ProcessResult:
        return await run_process(
            [
                self._ssh,
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={int(min(timeout_seconds, 30))}",
                f"{self._user}@{self._host}",
                script,
            ],
            timeout_seconds=timeout_seconds,
        )


def _project_name(deployment_id: UUID) -> str:
    return f"slipway-{deployment_id}"


def _parse_compose_ls(stdout: str) -> list[Deployment]:
    import json

    try:
        rows = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        # A malformed listing must not take down the reconciler; an empty
        # result makes it a no-op, which is the safe direction.
        log.warning("deploy.compose_ls_unparseable")
        return []

    live: list[Deployment] = []
    for row in rows:
        name = row.get("Name", "")
        if not name.startswith("slipway-"):
            continue
        try:
            deployment_id = UUID(name.removeprefix("slipway-"))
        except ValueError:
            continue
        live.append(
            Deployment(
                deployment_id=deployment_id,
                run_id=deployment_id,  # resolved against the database by the reconciler
                url="",
                project_name=name,
                artifact_uri="",
                healthy=row.get("Status", "").startswith("running"),
            )
        )
    return live
