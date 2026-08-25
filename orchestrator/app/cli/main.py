"""The Slipway CLI.

Every RUNBOOK.md procedure is a command here. Commands parse arguments, call
one service and print; there is no logic in this package (ADR 0002).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

from app.config import get_settings
from app.container import Container, build_container
from app.domain.entities import Gate, RunState
from app.domain.errors import SlipwayError
from app.logging import configure_logging

app = typer.Typer(help="Slipway operator CLI", no_args_is_help=True)
runs_app = typer.Typer(help="Inspect and drive runs", no_args_is_help=True)
models_app = typer.Typer(help="The models seam", no_args_is_help=True)
app.add_typer(runs_app, name="runs")
app.add_typer(models_app, name="models")

def _container() -> Container:
    settings = get_settings()
    configure_logging(settings)
    return build_container(settings)


@runs_app.command("create")
def runs_create(
    brief: Annotated[str, typer.Argument(help="The project brief, or - to read stdin")],
    title: Annotated[str, typer.Option(help="Short human label")] = "",
) -> None:
    """Start a run from a brief."""
    import sys

    text = sys.stdin.read() if brief == "-" else brief
    container = _container()

    async def go() -> None:
        try:
            run = await container.runs.create(text, title=title or None)
            print(f"{run.id}\t{run.state.value}")
        finally:
            await container.aclose()

    asyncio.run(go())


@runs_app.command("list")
def runs_list(
    state: Annotated[list[str] | None, typer.Option(help="Filter by state, repeatable")] = None,
    stuck: Annotated[bool, typer.Option(help="Only runs waiting on nothing")] = False,
    limit: int = 50,
) -> None:
    """List runs. `--stuck` is the first step of the runbook's stuck-run procedure."""
    container = _container()
    states = frozenset(RunState(s) for s in state) if state else None  # noqa: RUF100
    if stuck:
        states = frozenset(
            {RunState.SPECIFYING, RunState.BUILDING, RunState.TESTING, RunState.DEPLOYING}
        )

    async def go() -> None:
        try:
            for run in await container.runs.list_runs(states=states, limit=limit):
                print(
                    f"{run.id}\t{run.state.value:<14}\t"
                    f"{run.updated_at.isoformat()}\t{run.title or ''}"
                )
        finally:
            await container.aclose()

    asyncio.run(go())


@runs_app.command("show")
def runs_show(run_id: UUID, events: Annotated[int, typer.Option()] = 20) -> None:
    """Show a run and its most recent events."""
    container = _container()

    async def go() -> None:
        try:
            run = await container.runs.get(run_id)
            print(f"run       {run.id}")
            print(f"state     {run.state.value}")
            print(f"title     {run.title or '-'}")
            print(f"updated   {run.updated_at.isoformat()}")
            if run.failure_reason:
                print(f"failure   {run.failure_reason}")
            print("events:")
            for event in (await container.runs.events(run_id))[-events:]:
                print(
                    f"  {event.created_at.isoformat()}  "
                    f"{event.kind:<20} {json.dumps(event.payload)}"
                )
        finally:
            await container.aclose()

    asyncio.run(go())


@runs_app.command("approve")
def runs_approve(
    run_id: UUID,
    gate: Gate,
    by: Annotated[str, typer.Option(help="Who is approving")],
    note: str = "",
) -> None:
    """Approve a gate."""
    _decide(run_id, gate, approved=True, by=by, note=note)


@runs_app.command("reject")
def runs_reject(
    run_id: UUID,
    gate: Gate,
    by: Annotated[str, typer.Option(help="Who is rejecting")],
    note: Annotated[str, typer.Option(help="Why -- this is the agent's next input")] = "",
) -> None:
    """Reject a gate and send the run back a step."""
    _decide(run_id, gate, approved=False, by=by, note=note)


def _decide(run_id: UUID, gate: Gate, *, approved: bool, by: str, note: str) -> None:
    container = _container()

    async def go() -> None:
        try:
            run = await container.runs.decide(
                run_id, gate, approved=approved, decided_by=by, note=note or None
            )
            print(f"{run.id}\t{run.state.value}")
        finally:
            await container.aclose()

    asyncio.run(go())


@runs_app.command("cancel")
def runs_cancel(run_id: UUID, by: str, reason: str) -> None:
    """Cancel a run."""
    container = _container()

    async def go() -> None:
        try:
            run = await container.runs.cancel(run_id, actor=by, reason=reason)
            print(f"{run.id}\t{run.state.value}")
        finally:
            await container.aclose()

    asyncio.run(go())


@app.command("reconcile")
def reconcile(
    apply: Annotated[bool, typer.Option("--apply", help="Act on the report")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report only (default)")] = True,
) -> None:
    """Compare recorded state to actual state. Reports by default; acts only with --apply."""
    container = _container()

    async def go() -> None:
        try:
            report = await container.reconciler.inspect()
            for discrepancy in report.all:
                print(f"{discrepancy.kind:<12} {discrepancy.subject:<48} {discrepancy.detail}")
            if report.is_clean:
                print("clean")
                return
            if not apply:
                print(f"\n{len(report.all)} discrepancies. Re-run with --apply to act on them.")
                return
            after = await container.reconciler.apply(report)
            print(f"\napplied; {len(after.all)} remaining")
        finally:
            await container.aclose()

    asyncio.run(go())


@models_app.command("ping")
def models_ping() -> None:
    """One real call through the models seam. The provider-outage check."""
    container = _container()

    async def go() -> None:
        try:
            result = await container.models_service.ping()
            if result.ok:
                print(f"ok\t{result.model_id}\t{result.total_tokens} tokens")
            else:
                print(f"FAILED\t{result.detail}")
                raise typer.Exit(code=1)
        finally:
            await container.aclose()

    asyncio.run(go())


@models_app.command("catalogue")
def models_catalogue() -> None:
    """Show what config/models.yaml resolves to, and when it was synced."""
    container = _container()
    try:
        source, synced_at, specs = container.models_service.catalogue()
        print(f"source    {source or '-'}")
        print(f"synced    {synced_at}")
        for spec in specs:
            print(f"  {spec.role:<8} {spec.model_id:<48} ctx={spec.context_window}")
        if not specs:
            print("  (empty -- run `make models-sync`)")
    finally:
        asyncio.run(container.aclose())


@app.command("test")
def run_tests(
    deployment_id: UUID,
    suite: Annotated[Path, typer.Option("--suite", help="Directory holding the Playwright suite")],
    criteria: Annotated[
        str, typer.Option(help="Comma-separated acceptance criterion ids the spec declares")
    ] = "",
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Write the full report here")
    ] = None,
) -> None:
    """Run an acceptance suite against a deployment, and report by criterion."""
    container = _container()
    expected = tuple(c.strip() for c in criteria.split(",") if c.strip())

    async def go() -> None:
        try:
            report = await container.tests.run_suite(
                deployment_id, suite=suite, expected_criteria=expected
            )
        finally:
            await container.aclose()

        if json_out is not None:
            json_out.write_text(json.dumps(report.as_dict(), indent=2))

        if report.failure:
            print(f"the suite did not run: {report.failure}")
            raise typer.Exit(code=2)

        print(f"deployment  {report.deployment_id}")
        print(f"url         {report.base_url}  (network {report.network})")
        print(f"results     {report.results_dir}")
        print(f"duration    {report.duration_ms / 1000:.1f}s")
        print()
        print("BY CRITERION")
        for outcome in report.by_criterion:
            mark = {"verified": "PASS", "failed": "FAIL", "untested": "UNTESTED"}[outcome.status]
            print(
                f"  {mark:<9} {outcome.criterion:<10} "
                f"{outcome.tests_passed} passed, {outcome.tests_failed} failed"
            )
            for title in outcome.failing_tests:
                print(f"                       failing: {title}")
        if not report.by_criterion:
            print("  (no test declares a criterion)")
        print()
        print("TESTS")
        for test in report.tests:
            mark = "PASS" if test.ok else "FAIL"
            print(f"  {mark:<5} {test.duration_ms:>6}ms  {test.title}")
            if test.trace_path:
                print(f"           trace:      {test.trace_path}")
            if test.screenshot_path:
                print(f"           screenshot: {test.screenshot_path}")
            if test.error:
                first = test.error.splitlines()[0] if test.error.splitlines() else ""
                print(f"           error:      {first}")
        print()
        print(f"{report.passed} passed, {report.failed} failed -- "
              f"{'GREEN' if report.ok else 'NOT GREEN'}")
        if not report.ok:
            raise typer.Exit(code=1)

    asyncio.run(go())


@app.command("migrate")
def migrate() -> None:
    """Apply pending forward-only migrations."""
    container = _container()

    async def go() -> None:
        try:
            applied = await container.migrations.apply_pending()
            print("\n".join(applied) if applied else "nothing to apply")
        finally:
            await container.aclose()

    asyncio.run(go())


def main() -> None:
    try:
        app()
    except SlipwayError as exc:
        # A configuration or domain error should print one sentence, not a
        # traceback: the CLI is an operator tool and the operator is usually
        # reading it at an unpleasant hour.
        raise SystemExit(f"slipway: {exc}") from exc


if __name__ == "__main__":
    main()
