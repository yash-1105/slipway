"""Running an acceptance suite against a deployment, and reporting by criterion.

The by-criterion aggregation is the point. A list of test results tells you what
ran; the evaluator has to answer a different question -- was criterion 3
verified -- and that question has answers a test list does not: a criterion with
no test at all is *untested*, which is neither a pass nor a failure and must
never be reported as either.

Tests declare what they verify in their title:

    test("[AC-2] the health endpoint reports every dependency", ...)

One test may declare several. Playwright annotations of type `criterion` are
read too, for suites that prefer them.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import structlog

from app.domain.entities import DeploymentRecord, DeploymentStatus
from app.domain.repositories import UnitOfWork
from app.integrations.playwright import (
    Attachment,
    PlaywrightRunner,
    RunnerFailure,
    RunnerOutcome,
    host_path_for,
)

log = structlog.get_logger(__name__)

UowFactory = Callable[[], UnitOfWork]

#: `[AC-3]`, `[AC-3, AC-4]`, `@AC-3`. Deliberately permissive about the
#: separator and strict about the shape, so a typo reads as no criterion rather
#: than a criterion nobody declared.
CRITERION_PATTERN = re.compile(r"\b(AC-[A-Za-z0-9._-]+)\b")

TestStatus = Literal["passed", "failed", "timedOut", "skipped", "interrupted"]
CriterionStatus = Literal["verified", "failed", "untested"]


@dataclass(frozen=True, slots=True)
class TestOutcome:
    title: str
    file: str
    line: int
    status: TestStatus
    duration_ms: int
    criteria: tuple[str, ...]
    trace_path: str | None = None
    screenshot_path: str | None = None
    video_path: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("passed", "skipped")


@dataclass(frozen=True, slots=True)
class CriterionOutcome:
    """What the evaluator reads."""

    criterion: str
    status: CriterionStatus
    tests_passed: int
    tests_failed: int
    tests: tuple[str, ...] = ()
    #: Populated for a failure, so a reader does not have to cross-reference.
    failing_tests: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SuiteReport:
    deployment_id: UUID
    base_url: str
    network: str
    suite: str
    started_at: str
    duration_ms: int
    tests: tuple[TestOutcome, ...]
    by_criterion: tuple[CriterionOutcome, ...]
    results_dir: str
    #: Set when the suite could not be run at all -- distinct from it running
    #: and failing, which is a normal outcome with a report.
    failure: str | None = None

    @property
    def passed(self) -> int:
        return sum(1 for t in self.tests if t.status == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for t in self.tests if not t.ok)

    @property
    def ok(self) -> bool:
        """Green means every test passed AND every criterion is verified.

        A suite where all tests pass but a criterion has no test is not green:
        that criterion is unverified, and calling the run green would report it
        as satisfied.
        """
        return (
            self.failure is None
            and self.failed == 0
            and all(c.status == "verified" for c in self.by_criterion)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "deployment_id": str(self.deployment_id),
            "base_url": self.base_url,
            "network": self.network,
            "suite": self.suite,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "ok": self.ok,
            "failure": self.failure,
            "totals": {
                "tests": len(self.tests),
                "passed": self.passed,
                "failed": self.failed,
            },
            "results_dir": self.results_dir,
            "by_criterion": [
                {
                    "criterion": c.criterion,
                    "status": c.status,
                    "tests_passed": c.tests_passed,
                    "tests_failed": c.tests_failed,
                    "tests": list(c.tests),
                    "failing_tests": list(c.failing_tests),
                }
                for c in self.by_criterion
            ],
            "tests": [
                {
                    "title": t.title,
                    "file": t.file,
                    "line": t.line,
                    "status": t.status,
                    "duration_ms": t.duration_ms,
                    "criteria": list(t.criteria),
                    "trace": t.trace_path,
                    "screenshot": t.screenshot_path,
                    "video": t.video_path,
                    "error": t.error,
                }
                for t in self.tests
            ],
        }


def criteria_in(title: str, annotations: list[dict[str, Any]] | None = None) -> tuple[str, ...]:
    """Every criterion a test declares, from its title and its annotations."""
    found = list(CRITERION_PATTERN.findall(title))
    for annotation in annotations or []:
        if str(annotation.get("type", "")).lower() == "criterion":
            found.extend(CRITERION_PATTERN.findall(str(annotation.get("description", ""))))
    seen: list[str] = []
    for criterion in found:
        if criterion not in seen:
            seen.append(criterion)
    return tuple(seen)


def aggregate_by_criterion(
    tests: tuple[TestOutcome, ...], expected: tuple[str, ...] = ()
) -> tuple[CriterionOutcome, ...]:
    """Roll test results up to the criteria they verify.

    `expected` is the criteria the specification declares. Any that no test
    references appear as `untested` -- the case a list of test results cannot
    express, and the one that matters most: a criterion nobody tested is not a
    criterion that passed.
    """
    order: list[str] = []
    passed: dict[str, int] = {}
    failed: dict[str, int] = {}
    titles: dict[str, list[str]] = {}
    failing: dict[str, list[str]] = {}

    for criterion in expected:
        if criterion not in order:
            order.append(criterion)
            passed[criterion] = failed[criterion] = 0
            titles[criterion] = []
            failing[criterion] = []

    for test in tests:
        for criterion in test.criteria:
            if criterion not in order:
                order.append(criterion)
                passed[criterion] = failed[criterion] = 0
                titles[criterion] = []
                failing[criterion] = []
            titles[criterion].append(test.title)
            if test.ok:
                passed[criterion] += 1
            else:
                failed[criterion] += 1
                failing[criterion].append(test.title)

    outcomes: list[CriterionOutcome] = []
    for criterion in order:
        total = passed[criterion] + failed[criterion]
        if total == 0:
            status: CriterionStatus = "untested"
        elif failed[criterion]:
            status = "failed"
        else:
            status = "verified"
        outcomes.append(
            CriterionOutcome(
                criterion=criterion,
                status=status,
                tests_passed=passed[criterion],
                tests_failed=failed[criterion],
                tests=tuple(titles[criterion]),
                failing_tests=tuple(failing[criterion]),
            )
        )
    return tuple(outcomes)


def parse_report(report: dict[str, Any], results_dir: Path) -> tuple[TestOutcome, ...]:
    """Flatten Playwright's nested JSON into one result per test."""
    outcomes: list[TestOutcome] = []

    def walk(suite: dict[str, Any], file_hint: str) -> None:
        file_name = str(suite.get("file") or file_hint)
        for spec in suite.get("specs", []) or []:
            title = str(spec.get("title", ""))
            line = int(spec.get("line", 0) or 0)
            for test in spec.get("tests", []) or []:
                results = test.get("results", []) or []
                last = results[-1] if results else {}
                attachments = _attachments(last.get("attachments", []) or [], results_dir)
                error = last.get("error") or {}
                outcomes.append(
                    TestOutcome(
                        title=title,
                        file=file_name,
                        line=line,
                        status=str(last.get("status", test.get("status", "skipped"))),  # type: ignore[arg-type]
                        duration_ms=int(last.get("duration", 0) or 0),
                        criteria=criteria_in(title, test.get("annotations")),
                        trace_path=attachments.get("trace"),
                        screenshot_path=attachments.get("screenshot"),
                        video_path=attachments.get("video"),
                        error=_clean_error(error),
                    )
                )
        for nested in suite.get("suites", []) or []:
            walk(nested, file_name)

    for suite in report.get("suites", []) or []:
        walk(suite, "")
    return tuple(outcomes)


def _attachments(raw: list[dict[str, Any]], results_dir: Path) -> dict[str, str]:
    """Container paths translated to host paths, keeping only ones that exist.

    Reporting a trace path a reader cannot open is worse than reporting none:
    they go looking, and the absence looks like their mistake.
    """
    found: dict[str, str] = {}
    for attachment in raw:
        name = str(attachment.get("name", ""))
        path = str(attachment.get("path", ""))
        if not path or name in found:
            continue
        translated: Attachment | None = host_path_for(path, results_dir)
        if translated is None or not translated.exists or translated.host_path is None:
            continue
        found[name] = str(translated.host_path)
    return found


def _clean_error(error: dict[str, Any]) -> str | None:
    message = str(error.get("message", "")).strip()
    if not message:
        return None
    # Playwright colours its messages; the report is read as text and stored.
    return re.sub(r"\x1b\[[0-9;]*m", "", message)[:4000]


class TestService:
    def __init__(
        self,
        uow_factory: UowFactory,
        runner: PlaywrightRunner,
        *,
        timeout_seconds: float,
        preflight_timeout_seconds: float,
    ) -> None:
        self._uow = uow_factory
        self._runner = runner
        self._timeout = timeout_seconds
        self._preflight_timeout = preflight_timeout_seconds

    async def run_suite(
        self,
        deployment_id: UUID,
        *,
        suite: Path,
        expected_criteria: tuple[str, ...] = (),
    ) -> SuiteReport:
        started = datetime.now(UTC)
        record = await self._deployment(deployment_id)

        if record is None:
            return self._not_run(
                deployment_id, suite, started,
                f"no deployment {deployment_id}. Deploy first, or check the id.",
            )
        if record.destroyed_at is not None:
            return self._not_run(
                deployment_id, suite, started,
                f"deployment {deployment_id} was destroyed at {record.destroyed_at}. "
                "There is nothing to test.",
                record=record,
            )
        if record.status is not DeploymentStatus.LIVE:
            return self._not_run(
                deployment_id, suite, started,
                f"deployment {deployment_id} is {record.status.value}, not live. "
                "Only a live deployment can be tested.",
                record=record,
            )
        # Off the event loop: it stats the filesystem.
        if not await asyncio.to_thread(suite.is_dir):
            return self._not_run(
                deployment_id, suite, started,
                f"no suite at {suite}. Point --suite at a directory containing a "
                "Playwright config and its tests.",
                record=record,
            )

        network = record.network or ""
        base_url = f"http://{record.container_name}:{record.container_port}"

        image = await self._runner.ensure_image()
        if image is not None:
            return self._not_run(
                deployment_id, suite, started, f"{image.reason}: {image.detail}",
                record=record, base_url=base_url,
            )

        # Fail fast, before a suite's worth of timeouts.
        unreachable = await self._runner.reachable(
            network=network, base_url=base_url,
            timeout_seconds=self._preflight_timeout,
        )
        if unreachable is not None:
            log.warning("test.unreachable", deployment_id=str(deployment_id), url=base_url)
            return self._not_run(
                deployment_id, suite, started, unreachable.detail,
                record=record, base_url=base_url,
            )

        outcome = await self._runner.run(
            suite=suite, network=network, base_url=base_url,
            run_id=str(deployment_id), timeout_seconds=self._timeout,
        )
        if isinstance(outcome, RunnerFailure):
            return self._not_run(
                deployment_id, suite, started, f"{outcome.reason}: {outcome.detail}",
                record=record, base_url=base_url,
            )

        return self._report(deployment_id, record, suite, started, outcome, expected_criteria)

    async def _deployment(self, deployment_id: UUID) -> DeploymentRecord | None:
        async with self._uow() as uow:
            return await uow.deployments.get(deployment_id)

    def _report(
        self,
        deployment_id: UUID,
        record: DeploymentRecord,
        suite: Path,
        started: datetime,
        outcome: RunnerOutcome,
        expected: tuple[str, ...],
    ) -> SuiteReport:
        tests = parse_report(outcome.report, outcome.results_dir)
        return SuiteReport(
            deployment_id=deployment_id,
            base_url=f"http://{record.container_name}:{record.container_port}",
            network=record.network or "",
            suite=str(suite),
            started_at=started.isoformat(timespec="seconds"),
            duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
            tests=tests,
            by_criterion=aggregate_by_criterion(tests, expected),
            results_dir=str(outcome.results_dir),
        )

    def _not_run(
        self,
        deployment_id: UUID,
        suite: Path,
        started: datetime,
        failure: str,
        *,
        record: DeploymentRecord | None = None,
        base_url: str = "",
    ) -> SuiteReport:
        return SuiteReport(
            deployment_id=deployment_id,
            base_url=base_url,
            network=(record.network or "") if record else "",
            suite=str(suite),
            started_at=started.isoformat(timespec="seconds"),
            duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
            tests=(),
            by_criterion=(),
            results_dir="",
            failure=failure,
        )
