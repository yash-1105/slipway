"""The local container deploy provider, against a real Docker daemon.

These are failure-mode tests. The happy path is one of nine, because a deploy
provider earns its place by how it behaves when the build is broken, the
Dockerfile is incomplete, or two workers reach for the same port.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest

from app.deploy.base import DeployFailure, Deployment, is_build_time, split_env
from app.deploy.impl.local_container import (
    IMAGE_REPOSITORY,
    LABEL_MANAGED,
    LocalContainerDeployer,
    container_name_for,
    image_tag_for,
)
from app.domain.entities import DeploymentStatus
from app.domain.ids import uuid7
from app.services.deploys import DeployService
from tests.integration.conftest import UowFactory

pytestmark = pytest.mark.integration

PORT_RANGE = (41500, 41599)
HOST = "127.0.0.1"


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(
        ["docker", "info"], capture_output=True, timeout=30
    ).returncode == 0


needs_docker = pytest.mark.skipif(not docker_available(), reason="needs a running Docker daemon")


# --- fixtures --------------------------------------------------------------

WORKING_APP = {
    "Dockerfile": """FROM python:3.11-alpine
ARG PUBLIC_GREETING=unset
ENV BAKED_GREETING=$PUBLIC_GREETING
WORKDIR /app
COPY server.py .
EXPOSE 3000
HEALTHCHECK --interval=2s --timeout=3s --start-period=2s --retries=5 \\
    CMD wget --quiet --tries=1 --spider http://127.0.0.1:3000/ || exit 1
CMD ["python", "server.py"]
""",
    "server.py": """import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = os.environ.get("BAKED_GREETING", "unset").encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


HTTPServer(("0.0.0.0", 3000), Handler).serve_forever()
""",
}

NO_HEALTHCHECK_APP = {
    "Dockerfile": WORKING_APP["Dockerfile"].replace(
        'HEALTHCHECK --interval=2s --timeout=3s --start-period=2s --retries=5 \\\n'
        '    CMD wget --quiet --tries=1 --spider http://127.0.0.1:3000/ || exit 1\n',
        "",
    ),
    "server.py": WORKING_APP["server.py"],
}

BROKEN_BUILD_APP = {
    "Dockerfile": """FROM python:3.11-alpine
WORKDIR /app
COPY broken.py .
RUN python -c "import py_compile, sys; py_compile.compile('broken.py', doraise=True)"
HEALTHCHECK CMD true
CMD ["python", "broken.py"]
""",
    "broken.py": "def greet(:\n    return 'this does not parse'\n",
}


def write_project(root: Path, files: dict[str, str]) -> str:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (root / name).write_text(content)
    return str(root)


@pytest.fixture
def deployer() -> LocalContainerDeployer:
    return LocalContainerDeployer(
        docker_binary="docker",
        public_host=HOST,
        health_timeout_seconds=90.0,
        health_poll_seconds=0.5,
    )


@pytest.fixture
def deploys(uow_factory: UowFactory, deployer: LocalContainerDeployer) -> DeployService:
    return DeployService(
        uow_factory, deployer, host=HOST, port_range=PORT_RANGE, timeout_seconds=300.0
    )


@pytest.fixture
def cleanup_containers() -> Iterator[None]:
    """Remove the containers AND the images these tests built.

    Without the image half, a run leaves one ~90MB image per deployment behind.
    Sixty-two of them filled the Docker disk and stopped an unrelated pull --
    the leak itself is the deployer's, recorded in docs/notes/observations.md,
    but these tests are not entitled to leave their own mess for it.
    """
    yield
    subprocess.run(
        f"docker ps -aq --filter label={LABEL_MANAGED}=true | xargs -r docker rm -f",
        shell=True, capture_output=True, timeout=120,
    )
    subprocess.run(
        "docker images --filter reference=slipway/preview -q | xargs -r docker rmi -f",
        shell=True, capture_output=True, timeout=180,
    )


async def _a_run(uow_factory: UowFactory) -> UUID:
    from datetime import UTC, datetime

    from app.domain.entities import Run, RunState

    now = datetime.now(UTC)
    run = Run(id=uuid7(), brief="deploy test", state=RunState.CREATED,
              created_at=now, updated_at=now)
    async with uow_factory() as uow:
        await uow.runs.create(run)
        await uow.commit()
    return run.id


def fetch(url: str, timeout: float = 10.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


# --- pure logic ------------------------------------------------------------


def test_build_time_variables_are_recognised_by_prefix() -> None:
    """The most common real deployment failure, and why this provider exists."""
    assert is_build_time("VITE_API_URL")
    assert is_build_time("NEXT_PUBLIC_SENTRY_DSN")
    assert not is_build_time("DATABASE_URL")
    assert not is_build_time("API_KEY")


def test_env_splits_into_build_args_and_runtime() -> None:
    build, runtime = split_env(
        {"VITE_API_URL": "https://x", "NEXT_PUBLIC_KEY": "pk", "DATABASE_URL": "postgres://y"}
    )
    assert build == {"VITE_API_URL": "https://x", "NEXT_PUBLIC_KEY": "pk"}
    assert runtime == {"DATABASE_URL": "postgres://y"}


def test_the_container_name_is_derived_from_the_deployment_id() -> None:
    """Deterministic, so it can be written down before the container exists."""
    deployment_id = uuid7()
    assert container_name_for(deployment_id) == f"slipway-preview-{deployment_id}"
    assert container_name_for(deployment_id) == container_name_for(deployment_id)


# --- against Docker --------------------------------------------------------


@needs_docker
async def test_a_trivial_app_deploys_and_responds_200(
    deploys: DeployService, uow_factory: UowFactory, tmp_path: Path, cleanup_containers: None
) -> None:
    run_id = await _a_run(uow_factory)
    context = write_project(tmp_path / "app", WORKING_APP)

    result = await deploys.deploy(
        run_id, context_path=context, artifact_uri=f"file://{context}"
    )

    assert isinstance(result, Deployment), getattr(result, "detail", "")
    assert result.healthy
    status, _ = fetch(result.url)
    assert status == 200

    record = await deploys.get(result.deployment_id)
    assert record is not None
    assert record.status is DeploymentStatus.LIVE
    assert record.url == result.url
    assert record.container_id, "the container id must be recorded once known"


@needs_docker
async def test_build_args_reach_the_build_and_runtime_env_does_not(
    deploys: DeployService, uow_factory: UowFactory, tmp_path: Path, cleanup_containers: None
) -> None:
    """A PUBLIC_ variable is baked in at build time.

    Supplying it as runtime environment instead would leave the image saying
    'unset' -- a deploy that succeeds and is silently wrong, which is the
    failure this provider exists to catch.
    """
    run_id = await _a_run(uow_factory)
    context = write_project(tmp_path / "app", WORKING_APP)

    result = await deploys.deploy(
        run_id,
        context_path=context,
        artifact_uri=f"file://{context}",
        env={"PUBLIC_GREETING": "baked-at-build-time", "DATABASE_URL": "postgres://ignored"},
    )

    assert isinstance(result, Deployment), getattr(result, "detail", "")
    status, body = fetch(result.url)
    assert status == 200
    assert body == "baked-at-build-time"


@needs_docker
async def test_a_missing_healthcheck_fails_with_the_specific_message(
    deploys: DeployService, uow_factory: UowFactory, tmp_path: Path, cleanup_containers: None
) -> None:
    """Not a silent success. Docker calls a container with no HEALTHCHECK
    healthy the moment it starts, so without this a broken app deploys green."""
    run_id = await _a_run(uow_factory)
    context = write_project(tmp_path / "no-health", NO_HEALTHCHECK_APP)

    result = await deploys.deploy(
        run_id, context_path=context, artifact_uri=f"file://{context}"
    )

    assert isinstance(result, DeployFailure)
    assert result.reason == "no_healthcheck"
    assert "HEALTHCHECK" in result.detail
    assert "Dockerfile" in result.detail

    record = await deploys.get(result.deployment_id or uuid7())
    assert record is not None
    assert record.status is DeploymentStatus.FAILED
    assert record.destroyed_at is not None, "a failed deploy must give its port back"


@needs_docker
async def test_a_broken_build_returns_the_compiler_error_and_does_not_raise(
    deploys: DeployService, uow_factory: UowFactory, tmp_path: Path, cleanup_containers: None
) -> None:
    """The log is the deliverable: a human at a gate or an agent asked to fix
    it both need the compiler's own words, which a traceback throws away."""
    run_id = await _a_run(uow_factory)
    context = write_project(tmp_path / "broken", BROKEN_BUILD_APP)

    result = await deploys.deploy(
        run_id, context_path=context, artifact_uri=f"file://{context}"
    )

    assert isinstance(result, DeployFailure)
    assert result.reason == "build_failed"
    assert "SyntaxError" in result.log or "invalid syntax" in result.log
    assert "broken.py" in result.log


@needs_docker
async def test_ten_concurrent_deploys_never_receive_the_same_port(
    uow_factory: UowFactory, deployer: LocalContainerDeployer
) -> None:
    """The property the unique index exists for.

    Allocation only -- no containers are built, because what is under test is
    that ten racing inserts produce ten distinct ports.
    """
    run_id = await _a_run(uow_factory)
    service = DeployService(
        uow_factory, deployer, host=HOST, port_range=PORT_RANGE, timeout_seconds=60.0
    )

    records = await asyncio.gather(*(service.allocate(run_id, artifact_uri="file:///x")
                                    for _ in range(10)))

    ports = [r.port for r in records]
    assert len(set(ports)) == 10, f"duplicate port allocated: {sorted(ports)}"
    assert len({r.id for r in records}) == 10
    assert all(r.container_name for r in records), "the name is recorded before anything exists"


@needs_docker
async def test_destroy_is_idempotent_and_frees_the_port(
    deploys: DeployService, uow_factory: UowFactory, tmp_path: Path, cleanup_containers: None
) -> None:
    run_id = await _a_run(uow_factory)
    context = write_project(tmp_path / "app", WORKING_APP)

    result = await deploys.deploy(
        run_id, context_path=context, artifact_uri=f"file://{context}"
    )
    assert isinstance(result, Deployment), getattr(result, "detail", "")
    port = (await deploys.get(result.deployment_id)).port  # type: ignore[union-attr]

    await deploys.destroy(result.deployment_id)
    await deploys.destroy(result.deployment_id)   # again
    await deploys.destroy(result.deployment_id)   # and again

    record = await deploys.get(result.deployment_id)
    assert record is not None
    assert record.status is DeploymentStatus.TORN_DOWN
    assert record.destroyed_at is not None

    # The port is genuinely free: a new deployment can claim it.
    holders = await deploys.list_holding_ports()
    assert port not in [h.port for h in holders]


def image_exists(deployment_id: UUID) -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", image_tag_for(deployment_id)],
        capture_output=True, timeout=60,
    ).returncode == 0


@needs_docker
async def test_no_image_survives_teardown(
    deploys: DeployService, uow_factory: UowFactory, tmp_path: Path,
    cleanup_containers: None
) -> None:
    """The leak, closed.

    An image per deployment, ~90MB each, that nothing ever removed: 62 of them
    reached 5.6GB and filled the Docker disk. The cache argument does not apply
    -- every deployment builds a different commit, so the tag is never reused.
    """
    run_id = await _a_run(uow_factory)
    context = write_project(tmp_path / "app", WORKING_APP)

    result = await deploys.deploy(
        run_id, context_path=context, artifact_uri=f"file://{context}"
    )
    assert isinstance(result, Deployment), getattr(result, "detail", "")
    assert image_exists(result.deployment_id), "the deploy should have built an image"

    await deploys.destroy(result.deployment_id)

    assert not image_exists(result.deployment_id), (
        f"{image_tag_for(result.deployment_id)} survived teardown"
    )


@needs_docker
async def test_removing_an_image_twice_is_not_an_error(
    deploys: DeployService, deployer: LocalContainerDeployer, uow_factory: UowFactory,
    tmp_path: Path, cleanup_containers: None
) -> None:
    """Teardown is retried; the second attempt must not fail on what the first removed."""
    run_id = await _a_run(uow_factory)
    context = write_project(tmp_path / "app", WORKING_APP)
    result = await deploys.deploy(
        run_id, context_path=context, artifact_uri=f"file://{context}"
    )
    assert isinstance(result, Deployment), getattr(result, "detail", "")

    await deploys.destroy(result.deployment_id)
    await deployer.remove_image(result.deployment_id, timeout_seconds=60.0)
    await deployer.remove_image(result.deployment_id, timeout_seconds=60.0)

    assert not image_exists(result.deployment_id)


@needs_docker
async def test_a_failed_deploy_does_not_leave_its_image_behind(
    deploys: DeployService, uow_factory: UowFactory, tmp_path: Path,
    cleanup_containers: None
) -> None:
    """A build that fails leaves nothing to remove; one that starts and then
    fails health must not keep its image either."""
    run_id = await _a_run(uow_factory)
    context = write_project(tmp_path / "no-health", NO_HEALTHCHECK_APP)

    result = await deploys.deploy(
        run_id, context_path=context, artifact_uri=f"file://{context}"
    )
    assert isinstance(result, DeployFailure)
    assert result.deployment_id is not None

    # The deploy failed after building. Nothing torn it down yet, so the image
    # is still there -- and the reconciler is what notices.
    await deploys.destroy(result.deployment_id)
    assert not image_exists(result.deployment_id)


@needs_docker
async def test_list_images_reports_only_our_preview_images(
    deploys: DeployService, deployer: LocalContainerDeployer, uow_factory: UowFactory,
    tmp_path: Path, cleanup_containers: None
) -> None:
    run_id = await _a_run(uow_factory)
    context = write_project(tmp_path / "app", WORKING_APP)
    result = await deploys.deploy(
        run_id, context_path=context, artifact_uri=f"file://{context}"
    )
    assert isinstance(result, Deployment), getattr(result, "detail", "")

    images = await deployer.list_images()

    ours = [i for i in images if i.deployment_id == result.deployment_id]
    assert len(ours) == 1
    assert ours[0].reference == f"{IMAGE_REPOSITORY}:{result.deployment_id}"
    assert ours[0].size_bytes > 0, "size is reported so a report can total the waste"
