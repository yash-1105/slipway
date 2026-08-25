"""Running a Playwright suite in its own container.

The runner joins the deployment's Docker network and addresses it by container
name. Not host networking: the published port is a convenience for a human with
a browser, and reaching a deployment through the host is a code path that does
not exist on the server. One way of reaching it, everywhere.

Everything here returns values. A suite that fails is a normal outcome -- it is
the entire point of running it -- so nothing raises for a red test.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from app.integrations.process import run as run_process

log = structlog.get_logger(__name__)

#: Where the suite and its output are mounted inside the runner.
SUITE_MOUNT = "/suite"
RESULTS_MOUNT = "/results"

#: Installed into the image once, so `npm install` does not run per suite.
RUNNER_TOOLS = "/opt/playwright-tools"


@dataclass(frozen=True, slots=True)
class RunnerFailure:
    """The suite did not run. Distinct from the suite running and failing."""

    reason: str
    detail: str
    log: str = ""


@dataclass(frozen=True, slots=True)
class RunnerOutcome:
    """The suite ran. Whether it passed is in the report."""

    report: dict[str, Any]
    results_dir: Path
    stdout: str
    exit_code: int


RunnerResult = RunnerOutcome | RunnerFailure


class PlaywrightRunner:
    def __init__(
        self,
        *,
        docker_binary: str,
        image: str,
        results_root: Path,
    ) -> None:
        self._docker = docker_binary
        self._image = image
        self._results_root = results_root

    async def ensure_image(self, *, timeout_seconds: float = 900.0) -> RunnerFailure | None:
        """Build the runner image: the base image plus @playwright/test.

        The official image ships the browsers and Node, not the test runner
        package -- projects normally `npm ci` their own. Installing it into the
        image once means a suite does not pay for an install on every run, and
        an offline sandbox does not need a registry to run tests.
        """
        tag = self.runner_tag
        present = await run_process(
            [self._docker, "image", "inspect", tag], timeout_seconds=60.0
        )
        if present.ok:
            return None

        version = self._image.rsplit(":v", 1)[-1].split("-")[0]
        dockerfile = (
            f"FROM {self._image}\n"
            f"WORKDIR {RUNNER_TOOLS}\n"
            f"RUN npm init -y >/dev/null "
            f"&& npm install --no-audit --no-fund @playwright/test@{version}\n"
            f"ENV PATH={RUNNER_TOOLS}/node_modules/.bin:$PATH\n"
            f"WORKDIR {SUITE_MOUNT}\n"
        )
        log.info("test.building_runner_image", tag=tag, base=self._image)
        built = await run_process(
            [self._docker, "build", "--tag", tag, "-"],
            timeout_seconds=timeout_seconds,
            stdin=dockerfile.encode(),
        )
        if built.ok:
            return None
        return RunnerFailure(
            "runner_image",
            f"could not build the Playwright runner image from {self._image}",
            log=(built.stdout + built.stderr).strip(),
        )

    @property
    def runner_tag(self) -> str:
        version = self._image.rsplit(":", 1)[-1]
        return f"slipway/playwright-runner:{version}"

    async def reachable(
        self, *, network: str, base_url: str, timeout_seconds: float
    ) -> RunnerFailure | None:
        """One request from the network, before spending a suite's worth of time.

        Without this, an unreachable deployment surfaces as every test timing
        out in turn -- minutes of output whose real cause is one line. This
        turns that into one message in seconds.
        """
        script = (
            "const u=process.argv[1];"
            "const c=new AbortController();"
            f"const t=setTimeout(()=>c.abort(),{int(timeout_seconds * 1000)});"
            "fetch(u,{signal:c.signal})"
            ".then(r=>{clearTimeout(t);console.log('status',r.status);"
            "process.exit(r.status<500?0:1)})"
            ".catch(e=>{clearTimeout(t);console.error(String(e&&e.message||e));process.exit(1)});"
        )
        result = await run_process(
            [
                self._docker, "run", "--rm",
                "--network", network,
                self.runner_tag,
                "node", "-e", script, base_url,
            ],
            timeout_seconds=timeout_seconds + 20.0,
        )
        if result.ok:
            return None

        detail = (result.stdout + result.stderr).strip().splitlines()
        message = detail[-1] if detail else "no response"
        return RunnerFailure(
            "unreachable",
            (
                f"nothing answered at {base_url} from the {network} network: {message}. "
                "The deployment's container is not running, is not on this network, "
                "or is not listening on that port. Not running the suite."
            ),
            log="\n".join(detail[-20:]),
        )

    async def run(
        self,
        *,
        suite: Path,
        network: str,
        base_url: str,
        run_id: str,
        timeout_seconds: float,
    ) -> RunnerResult:
        results_dir = self._results_root / run_id
        # Off the event loop: rmtree of a previous run's traces is not fast, and
        # the worker has other jobs. `resolve()` goes with it -- it stats the
        # filesystem too, and both mount paths are built from the result.
        results_dir, suite_path = await asyncio.to_thread(
            _prepare_results_dir, results_dir, suite
        )

        report_name = "report.json"
        argv = [
            self._docker, "run", "--rm",
            "--network", network,
            "--volume", f"{suite_path}:{SUITE_MOUNT}:ro",
            "--volume", f"{results_dir}:{RESULTS_MOUNT}",
            "--env", f"BASE_URL={base_url}",
            # The suite is mounted read-only and has no node_modules of
            # its own, so its `import ... from "@playwright/test"`
            # resolves from /suite and finds nothing. NODE_PATH points
            # Node at the copy installed in the image.
            "--env", f"NODE_PATH={RUNNER_TOOLS}/node_modules",
            "--env", "CI=1",
            "--env", f"PLAYWRIGHT_JSON_OUTPUT_NAME={RESULTS_MOUNT}/{report_name}",
            "--workdir", SUITE_MOUNT,
            self.runner_tag,
            "playwright", "test",
            "--reporter=json",
            f"--output={RESULTS_MOUNT}/artifacts",
        ]

        log.info("test.running", base_url=base_url, network=network, suite=str(suite))
        result = await run_process(argv, timeout_seconds=timeout_seconds)

        report_path = results_dir / report_name
        if not report_path.is_file():
            return RunnerFailure(
                "no_report",
                (
                    "Playwright produced no JSON report. The suite may not have "
                    "compiled, or its config may not accept BASE_URL."
                ),
                log=(result.stdout + result.stderr).strip()[-6000:],
            )

        try:
            report = json.loads(report_path.read_text())
        except json.JSONDecodeError as exc:
            return RunnerFailure(
                "bad_report",
                f"the JSON report did not parse: {exc}",
                log=report_path.read_text()[:4000],
            )

        return RunnerOutcome(
            report=report,
            results_dir=results_dir,
            stdout=(result.stdout + result.stderr).strip()[-6000:],
            exit_code=result.exit_code,
        )


def _prepare_results_dir(results_dir: Path, suite: Path) -> tuple[Path, Path]:
    """Clean the results directory and resolve both mount paths.

    A run gets a fresh directory: a stale trace read as this run's is worse than
    a missing one.
    """
    if results_dir.exists():
        shutil.rmtree(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    return results_dir.resolve(), suite.resolve()


@dataclass(frozen=True, slots=True)
class Attachment:
    name: str
    container_path: str
    host_path: Path | None = None
    exists: bool = False


def host_path_for(container_path: str, results_dir: Path) -> Attachment | None:
    """Translate a path inside the runner to one on this machine.

    Playwright records the path it saw. Reporting that to a human who then
    cannot open it is worse than reporting nothing, so the translation happens
    here and the result records whether the file is really there.
    """
    if not container_path.startswith(RESULTS_MOUNT):
        return None
    relative = container_path[len(RESULTS_MOUNT) :].lstrip("/")
    host = results_dir / relative
    return Attachment(
        name="", container_path=container_path, host_path=host, exists=host.is_file()
    )
