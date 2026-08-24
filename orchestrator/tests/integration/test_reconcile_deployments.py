"""Reconciliation, both directions, and the destructive half.

`apply()` was at 0% coverage. It is the half that stops containers, tears down
deployments and releases ports, and it has already had one bug where it treated
a live deployment as an orphan. That class of bug has to be caught by a test,
not by someone reading the code carefully enough.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from app.deploy.base import Deployment
from app.deploy.impl.local_container import (
    LABEL_DEPLOYMENT,
    LABEL_MANAGED,
    LABEL_RUN,
    LocalContainerDeployer,
    container_name_for,
)
from app.domain.entities import DeploymentStatus, Run, RunState, Trigger
from app.domain.ids import uuid7
from app.services.deploys import DeployService
from app.services.reconcile import Reconciler
from app.services.runs import RunService
from tests.integration.conftest import UowFactory

pytestmark = pytest.mark.integration

PORT_RANGE = (41700, 41799)
HOST = "127.0.0.1"


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


needs_docker = pytest.mark.skipif(not docker_available(), reason="needs a running Docker daemon")

TINY_APP = {
    "Dockerfile": """FROM python:3.11-alpine
WORKDIR /app
COPY server.py .
EXPOSE 3000
HEALTHCHECK --interval=2s --timeout=3s --start-period=2s --retries=5 \\
    CMD wget --quiet --tries=1 --spider http://127.0.0.1:3000/ || exit 1
CMD ["python", "server.py"]
""",
    "server.py": """from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


HTTPServer(("0.0.0.0", 3000), Handler).serve_forever()
""",
}


@pytest.fixture
def deployer() -> LocalContainerDeployer:
    return LocalContainerDeployer(
        docker_binary="docker", public_host=HOST,
        health_timeout_seconds=90.0, health_poll_seconds=0.5,
    )


@pytest.fixture
def deploys(uow_factory: UowFactory, deployer: LocalContainerDeployer) -> DeployService:
    return DeployService(
        uow_factory, deployer, host=HOST, port_range=PORT_RANGE, timeout_seconds=300.0
    )


@pytest.fixture
def reconciler(uow_factory: UowFactory, deployer: LocalContainerDeployer) -> Reconciler:
    from app.sandbox.impl.fake import FakeSandbox

    return Reconciler(
        uow_factory, FakeSandbox(), deployer,
        deploy_host=HOST, teardown_timeout_seconds=120.0,
    )


@pytest.fixture
def cleanup_containers() -> Iterator[None]:
    yield
    subprocess.run(
        f"docker ps -aq --filter label={LABEL_MANAGED}=true | xargs -r docker rm -f",
        shell=True, capture_output=True, timeout=120,
    )


async def a_live_run(uow_factory: UowFactory) -> UUID:
    """A run that is genuinely in flight, so its deployment is entitled to exist."""
    runs = RunService(uow_factory)
    run = await runs.create("reconcile test")
    await runs.advance(run.id, Trigger.AGENT_SUCCEEDED, actor="test")
    return run.id


async def a_dead_run(uow_factory: UowFactory) -> UUID:
    now = datetime.now(UTC)
    run = Run(id=uuid7(), brief="cancelled", state=RunState.CANCELLED,
              created_at=now, updated_at=now)
    async with uow_factory() as uow:
        await uow.runs.create(run)
        await uow.commit()
    return run.id


def write_project(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in TINY_APP.items():
        (root / name).write_text(content)
    return str(root)


def container_exists(name: str) -> bool:
    return subprocess.run(
        ["docker", "inspect", name], capture_output=True, timeout=30
    ).returncode == 0


# --- direction 1: a container created behind our back ----------------------


@needs_docker
async def test_reconcile_detects_a_container_created_behind_its_back(
    reconciler: Reconciler, cleanup_containers: None
) -> None:
    """Nothing in the store claims it, so nothing will ever clean it up."""
    stray_id = uuid7()
    name = container_name_for(stray_id)
    subprocess.run(
        ["docker", "run", "--detach", "--name", name,
         "--label", f"{LABEL_MANAGED}=true",
         "--label", f"{LABEL_DEPLOYMENT}={stray_id}",
         "--label", f"{LABEL_RUN}={uuid7()}",
         "alpine:3.20", "sleep", "300"],
        capture_output=True, timeout=180, check=True,
    )

    report = await reconciler.inspect()

    subjects = {d.subject for d in report.orphan_deployments}
    assert name in subjects, f"stray container not detected; saw {subjects}"
    assert any(d.deployment_id == stray_id for d in report.orphan_deployments)

    await reconciler.apply(report)

    assert not container_exists(name), "apply() must remove a container nothing claims"


# --- direction 2: a record whose container was removed by hand -------------


@needs_docker
async def test_reconcile_marks_destroyed_a_record_whose_container_was_removed(
    deploys: DeployService, reconciler: Reconciler, uow_factory: UowFactory,
    tmp_path: Path, cleanup_containers: None
) -> None:
    """The record is the only thing still holding that port."""
    run_id = await a_live_run(uow_factory)
    context = write_project(tmp_path / "app")
    result = await deploys.deploy(run_id, context_path=context, artifact_uri=f"file://{context}")
    assert isinstance(result, Deployment), getattr(result, "detail", "")

    record = await deploys.get(result.deployment_id)
    assert record is not None and record.destroyed_at is None
    port = record.port

    # A human removes it, or a `docker system prune` does.
    subprocess.run(
        ["docker", "rm", "--force", record.container_name or ""],
        capture_output=True, timeout=60, check=True,
    )

    report = await reconciler.inspect()
    assert any(d.deployment_id == result.deployment_id for d in report.vanished_deployments), (
        f"vanished container not detected; saw {[d.subject for d in report.all]}"
    )

    await reconciler.apply(report)

    settled = await deploys.get(result.deployment_id)
    assert settled is not None
    assert settled.status is DeploymentStatus.TORN_DOWN
    assert settled.destroyed_at is not None, "the port must be released"
    assert port not in [h.port for h in await deploys.list_holding_ports()]


# --- the safety property ---------------------------------------------------


@needs_docker
async def test_apply_does_not_destroy_a_live_deployment_of_a_running_deploy(
    deploys: DeployService, reconciler: Reconciler, uow_factory: UowFactory,
    tmp_path: Path, cleanup_containers: None
) -> None:
    """The bug that has already happened once, now caught by a test.

    A deployment belonging to a run that is still going is not an orphan. An
    earlier reconciler classified live deployments as orphans and would have
    torn down every application Slipway had shipped.
    """
    run_id = await a_live_run(uow_factory)
    context = write_project(tmp_path / "app")
    result = await deploys.deploy(run_id, context_path=context, artifact_uri=f"file://{context}")
    assert isinstance(result, Deployment), getattr(result, "detail", "")

    record = await deploys.get(result.deployment_id)
    assert record is not None
    name = record.container_name
    assert name and container_exists(name)

    report = await reconciler.inspect()

    # It must not appear in either destructive bucket.
    assert not any(d.deployment_id == result.deployment_id for d in report.orphan_deployments), (
        "a live deployment of a running run was classified as an orphan"
    )
    assert not any(d.deployment_id == result.deployment_id for d in report.vanished_deployments)

    await reconciler.apply(report)

    assert container_exists(name), "apply() destroyed a live deployment"
    still = await deploys.get(result.deployment_id)
    assert still is not None
    assert still.status is DeploymentStatus.LIVE
    assert still.destroyed_at is None, "apply() released a port that is still in use"

    # And it is still serving.
    import urllib.request

    with urllib.request.urlopen(result.url, timeout=10) as response:
        assert response.status == 200


@needs_docker
async def test_apply_does_tear_down_a_deployment_whose_run_was_cancelled(
    deploys: DeployService, reconciler: Reconciler, uow_factory: UowFactory,
    tmp_path: Path, cleanup_containers: None
) -> None:
    """The other half of the rule: a run that did not survive owns nothing.

    Without this, the safety test above could be satisfied by a reconciler that
    never destroys anything at all.
    """
    run_id = await a_dead_run(uow_factory)
    context = write_project(tmp_path / "app")
    result = await deploys.deploy(run_id, context_path=context, artifact_uri=f"file://{context}")
    assert isinstance(result, Deployment), getattr(result, "detail", "")

    record = await deploys.get(result.deployment_id)
    assert record is not None and record.container_name
    name = record.container_name

    report = await reconciler.inspect()
    assert any(d.deployment_id == result.deployment_id for d in report.orphan_deployments)

    await reconciler.apply(report)

    assert not container_exists(name)


@needs_docker
async def test_apply_is_idempotent(
    reconciler: Reconciler, cleanup_containers: None
) -> None:
    """Running it twice must not fail on what the first run already removed."""
    stray_id = uuid7()
    name = container_name_for(stray_id)
    subprocess.run(
        ["docker", "run", "--detach", "--name", name,
         "--label", f"{LABEL_MANAGED}=true",
         "--label", f"{LABEL_DEPLOYMENT}={stray_id}",
         "--label", f"{LABEL_RUN}={uuid7()}",
         "alpine:3.20", "sleep", "300"],
        capture_output=True, timeout=180, check=True,
    )

    first = await reconciler.inspect()
    after = await reconciler.apply(first)
    again = await reconciler.apply(after)

    assert not container_exists(name)
    assert again.is_clean or not any(d.subject == name for d in again.all)


@needs_docker
async def test_a_clean_system_reports_clean(reconciler: Reconciler) -> None:
    report = await reconciler.inspect()
    assert report.is_clean, [(d.kind, d.subject, d.detail) for d in report.all]
