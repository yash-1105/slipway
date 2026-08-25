"""The Playwright harness, against a real deployment on a real Docker network.

Three properties matter here: a green suite produces traces that exist on disk,
a red suite produces a failure with a trace you can actually open, and an
unreachable deployment fails in seconds with one message rather than a cascade
of per-test timeouts.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from app.deploy.base import Deployment
from app.deploy.impl.local_container import LABEL_MANAGED, LocalContainerDeployer
from app.domain.entities import Run, RunState
from app.domain.ids import uuid7
from app.integrations.playwright import PlaywrightRunner
from app.services.deploys import DeployService
from app.services.testing import TestService
from tests.integration.conftest import UowFactory

pytestmark = pytest.mark.integration

HOST = "127.0.0.1"
NETWORK = "slipway-test-harness"
PORT_RANGE = (41800, 41899)
IMAGE = "mcr.microsoft.com/playwright:v1.62.1-noble"


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


def image_present() -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True, timeout=60
    ).returncode == 0


needs_docker = pytest.mark.skipif(not docker_available(), reason="needs a running Docker daemon")
needs_image = pytest.mark.skipif(
    not image_present(), reason=f"needs {IMAGE}; run `docker pull {IMAGE}`"
)

APP = {
    "Dockerfile": """FROM python:3.11-alpine
WORKDIR /app
COPY server.py .
EXPOSE 3000
HEALTHCHECK --interval=2s --timeout=3s --start-period=2s --retries=5 \\
    CMD wget --quiet --tries=1 --spider http://127.0.0.1:3000/ || exit 1
CMD ["python", "server.py"]
""",
    "server.py": """from http.server import BaseHTTPRequestHandler, HTTPServer

PAGE = b"<html><body><h1 id='title'>Acme console</h1><p id='count'>3 items</p></body></html>"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"ok"}' if self.path == "/api/health" else PAGE
        kind = "application/json" if self.path == "/api/health" else "text/html"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


HTTPServer(("0.0.0.0", 3000), Handler).serve_forever()
""",
}

CONFIG = """import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  // No webServer: the thing under test is already deployed, and BASE_URL points
  // at it by container name on the shared Docker network.
  use: {
    baseURL: process.env.BASE_URL,
    trace: "on",
    screenshot: "only-on-failure",
  },
  reporter: [["json"]],
  timeout: 20000,
});
"""

PASSING_SUITE = """import { expect, test } from "@playwright/test";

test("[AC-1] the console renders its title", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#title")).toHaveText("Acme console");
});

test("[AC-2] the health endpoint answers", async ({ request }) => {
  const response = await request.get("/api/health");
  expect(response.status()).toBe(200);
});
"""

FAILING_SUITE = """import { expect, test } from "@playwright/test";

test("[AC-1] the console renders its title", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#title")).toHaveText("Acme console");
});

test("[AC-2] the item count is four", async ({ page }) => {
  await page.goto("/");
  // Deliberately wrong: the page says 3.
  await expect(page.locator("#count")).toHaveText("4 items");
});
"""


def write(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (root / name).write_text(content)
    return root


@pytest.fixture
def deployer() -> LocalContainerDeployer:
    return LocalContainerDeployer(
        docker_binary="docker", public_host=HOST, network=NETWORK,
        health_timeout_seconds=90.0, health_poll_seconds=0.5,
    )


@pytest.fixture
def deploys(uow_factory: UowFactory, deployer: LocalContainerDeployer) -> DeployService:
    return DeployService(
        uow_factory, deployer, host=HOST, port_range=PORT_RANGE,
        timeout_seconds=300.0, network=NETWORK,
    )


@pytest.fixture
def tests_service(uow_factory: UowFactory, tmp_path: Path) -> TestService:
    return TestService(
        uow_factory,
        PlaywrightRunner(
            docker_binary="docker", image=IMAGE, results_root=tmp_path / "results"
        ),
        timeout_seconds=600.0,
        preflight_timeout_seconds=15.0,
    )


@pytest.fixture
def cleanup() -> Iterator[None]:
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


async def a_run(uow_factory: UowFactory) -> UUID:
    now = datetime.now(UTC)
    run = Run(id=uuid7(), brief="harness", state=RunState.CREATED,
              created_at=now, updated_at=now)
    async with uow_factory() as uow:
        await uow.runs.create(run)
        await uow.commit()
    return run.id


async def deploy_app(deploys: DeployService, uow_factory: UowFactory, tmp_path: Path) -> UUID:
    run_id = await a_run(uow_factory)
    context = write(tmp_path / "app", APP)
    result = await deploys.deploy(
        run_id, context_path=str(context), artifact_uri=f"file://{context}"
    )
    assert isinstance(result, Deployment), getattr(result, "detail", "")
    return result.deployment_id


# --- the three properties --------------------------------------------------


@needs_docker
@needs_image
async def test_a_passing_suite_reports_pass_with_traces_on_disk(
    deploys: DeployService, tests_service: TestService, uow_factory: UowFactory,
    tmp_path: Path, cleanup: None
) -> None:
    deployment_id = await deploy_app(deploys, uow_factory, tmp_path)
    suite = write(
        tmp_path / "green",
        {"playwright.config.ts": CONFIG, "spec.spec.ts": PASSING_SUITE},
    )

    report = await tests_service.run_suite(
        deployment_id, suite=suite, expected_criteria=("AC-1", "AC-2")
    )

    assert report.failure is None, report.failure
    assert report.ok
    assert report.passed == 2
    assert report.failed == 0

    assert {c.criterion: c.status for c in report.by_criterion} == {
        "AC-1": "verified", "AC-2": "verified"
    }

    # The traces have to be openable, not merely mentioned.
    traced = [t for t in report.tests if t.trace_path]
    assert traced, "no test reported a trace"
    for test in traced:
        assert Path(test.trace_path or "").is_file(), f"trace missing: {test.trace_path}"


@needs_docker
@needs_image
async def test_a_failing_suite_reports_the_failure_with_a_usable_trace(
    deploys: DeployService, tests_service: TestService, uow_factory: UowFactory,
    tmp_path: Path, cleanup: None
) -> None:
    deployment_id = await deploy_app(deploys, uow_factory, tmp_path)
    suite = write(
        tmp_path / "red",
        {"playwright.config.ts": CONFIG, "spec.spec.ts": FAILING_SUITE},
    )

    report = await tests_service.run_suite(
        deployment_id, suite=suite, expected_criteria=("AC-1", "AC-2")
    )

    assert report.failure is None, "the suite ran; it just failed"
    assert not report.ok
    assert report.passed == 1
    assert report.failed == 1

    by_criterion = {c.criterion: c for c in report.by_criterion}
    assert by_criterion["AC-1"].status == "verified"
    assert by_criterion["AC-2"].status == "failed"
    assert by_criterion["AC-2"].failing_tests, "the failing test must be named"

    failed = [t for t in report.tests if not t.ok]
    assert len(failed) == 1
    assert failed[0].error and "4 items" in failed[0].error
    assert failed[0].trace_path and Path(failed[0].trace_path).is_file()
    assert failed[0].screenshot_path and Path(failed[0].screenshot_path).is_file()


@needs_docker
@needs_image
async def test_an_unreachable_deployment_fails_fast_with_a_clear_message(
    deploys: DeployService, tests_service: TestService, uow_factory: UowFactory,
    tmp_path: Path, cleanup: None
) -> None:
    """One message in seconds, not a cascade of per-test timeouts."""
    deployment_id = await deploy_app(deploys, uow_factory, tmp_path)
    record = await deploys.get(deployment_id)
    assert record is not None and record.container_name

    # The container goes away; the record still says live.
    subprocess.run(
        ["docker", "rm", "--force", record.container_name],
        capture_output=True, timeout=60, check=True,
    )

    suite = write(
        tmp_path / "green",
        {"playwright.config.ts": CONFIG, "spec.spec.ts": PASSING_SUITE},
    )

    started = time.monotonic()
    report = await tests_service.run_suite(deployment_id, suite=suite)
    elapsed = time.monotonic() - started

    assert report.failure is not None
    assert record.container_name in report.failure
    assert "not on this network" in report.failure or "not running" in report.failure
    assert report.tests == (), "no test should have run"
    assert elapsed < 90, f"took {elapsed:.0f}s; this must fail fast, not time out per test"


# --- the guards that do not need the image ---------------------------------


@needs_docker
async def test_testing_a_deployment_that_does_not_exist_says_so(
    tests_service: TestService, tmp_path: Path
) -> None:
    report = await tests_service.run_suite(uuid7(), suite=tmp_path)
    assert report.failure is not None
    assert "no deployment" in report.failure


@needs_docker
async def test_testing_a_destroyed_deployment_says_so(
    deploys: DeployService, tests_service: TestService, uow_factory: UowFactory,
    tmp_path: Path, cleanup: None
) -> None:
    deployment_id = await deploy_app(deploys, uow_factory, tmp_path)
    await deploys.destroy(deployment_id)

    report = await tests_service.run_suite(deployment_id, suite=tmp_path)

    assert report.failure is not None
    assert "destroyed" in report.failure
    assert report.tests == ()


@needs_docker
async def test_a_missing_suite_directory_says_so(
    deploys: DeployService, tests_service: TestService, uow_factory: UowFactory,
    tmp_path: Path, cleanup: None
) -> None:
    deployment_id = await deploy_app(deploys, uow_factory, tmp_path)

    report = await tests_service.run_suite(deployment_id, suite=tmp_path / "nope")

    assert report.failure is not None
    assert "no suite at" in report.failure
