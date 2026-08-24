"""A brief goes in, a deployed run comes out, pausing at both human gates."""

from __future__ import annotations

import pytest

from app.container import Container
from app.domain.entities import Gate, RunState
from app.worker import Worker

pytestmark = pytest.mark.acceptance


async def drain(worker: Worker, container: Container, *, limit: int = 20) -> int:
    """Run every currently-claimable job, once each. Returns how many ran."""
    kinds = frozenset(container.settings.worker_kinds)
    ran = 0
    for _ in range(limit):
        job = await container.jobs.claim(kinds)
        if job is None:
            return ran
        await worker._run_job(job)
        ran += 1
    raise AssertionError(f"worker did not settle within {limit} jobs")


async def test_the_whole_loop_from_brief_to_deployed(
    container: Container, worker: Worker
) -> None:
    run = await container.runs.create("a booking form for a dentist", title="acme")
    assert run.state is RunState.SPECIFYING

    # The specify agent runs and the run stops at the first gate.
    assert await drain(worker, container) == 1
    run = await container.runs.get(run.id)
    assert run.state is RunState.SPEC_REVIEW

    # Nothing moves without a human.
    assert await drain(worker, container) == 0
    assert (await container.runs.get(run.id)).state is RunState.SPEC_REVIEW

    run = await container.runs.decide(run.id, Gate.SPEC, approved=True, decided_by="yash")
    assert run.state is RunState.BUILDING

    # Build, then test, then the second gate.
    await drain(worker, container)
    run = await container.runs.get(run.id)
    assert run.state is RunState.DEPLOY_REVIEW

    assert await drain(worker, container) == 0, "the deploy gate must hold too"

    run = await container.runs.decide(run.id, Gate.DEPLOY, approved=True, decided_by="yash")
    assert run.state is RunState.DEPLOYING

    await drain(worker, container)
    run = await container.runs.get(run.id)
    assert run.state is RunState.DEPLOYED
    assert run.is_terminal


async def test_the_spec_gate_can_send_the_run_back(container: Container, worker: Worker) -> None:
    run = await container.runs.create("a brief")
    await drain(worker, container)

    run = await container.runs.decide(
        run.id, Gate.SPEC, approved=False, decided_by="yash", note="no auth flow"
    )
    assert run.state is RunState.SPECIFYING

    await drain(worker, container)
    run = await container.runs.get(run.id)
    assert run.state is RunState.SPEC_REVIEW, "a rejected spec is respecified, not failed"


async def test_the_deploy_gate_can_send_the_run_back_to_building(
    container: Container, worker: Worker
) -> None:
    run = await container.runs.create("a brief")
    await drain(worker, container)
    await container.runs.decide(run.id, Gate.SPEC, approved=True, decided_by="yash")
    await drain(worker, container)

    run = await container.runs.decide(
        run.id, Gate.DEPLOY, approved=False, decided_by="yash", note="the form does not submit"
    )
    assert run.state is RunState.BUILDING

    ran = await drain(worker, container)
    assert ran >= 1, "rejecting at the deploy gate must enqueue a real rebuild"
    run = await container.runs.get(run.id)
    assert run.state is RunState.DEPLOY_REVIEW


async def test_a_run_produces_an_audit_trail_a_human_can_read(
    container: Container, worker: Worker
) -> None:
    """The gates are judged against the event log, so it must tell the story."""
    run = await container.runs.create("a brief")
    await drain(worker, container)
    await container.runs.decide(
        run.id, Gate.SPEC, approved=True, decided_by="yash", note="ship it"
    )
    await drain(worker, container)

    events = await container.runs.events(run.id)
    kinds = [event.kind for event in events]

    assert kinds[0] == "run.created"
    assert "run.transitioned" in kinds
    assert "job.succeeded" in kinds

    approval = next(
        event
        for event in events
        if event.kind == "run.transitioned" and event.payload.get("actor") == "yash"
    )
    assert approval.payload["from"] == "spec_review"
    assert approval.payload["to"] == "building"
    assert approval.payload["detail"] == "ship it"


async def test_agent_output_is_stored_as_an_artifact(
    container: Container, worker: Worker
) -> None:
    run = await container.runs.create("a brief")
    await drain(worker, container)

    async with container.uow() as uow:
        artifacts = await uow.artifacts.list_for_run(run.id)

    assert [a.kind for a in artifacts] == ["spec"]
    assert artifacts[0].uri.startswith("memory://")
    assert len(artifacts[0].sha256) == 64

    content = await container.artifacts.get(artifacts[0].uri, timeout_seconds=5.0)
    assert isinstance(content, bytes)
    assert content


async def test_cancelling_mid_flight_stops_the_run(container: Container, worker: Worker) -> None:
    run = await container.runs.create("a brief")
    await drain(worker, container)

    run = await container.runs.cancel(run.id, actor="yash", reason="client pulled it")
    assert run.state is RunState.CANCELLED

    assert await drain(worker, container) == 0
    assert (await container.runs.get(run.id)).state is RunState.CANCELLED


async def test_a_deploy_allocates_a_port_and_a_url(container: Container, worker: Worker) -> None:
    run = await container.runs.create("a brief")
    await drain(worker, container)
    await container.runs.decide(run.id, Gate.SPEC, approved=True, decided_by="yash")
    await drain(worker, container)
    await container.runs.decide(run.id, Gate.DEPLOY, approved=True, decided_by="yash")
    await drain(worker, container)

    async with container.uow() as uow:
        allocations = await uow.ports.list_active(host="127.0.0.1")

    assert len(allocations) == 1
    assert allocations[0].run_id == run.id
    assert (
        container.settings.deploy_port_range_start
        <= allocations[0].port
        <= container.settings.deploy_port_range_end
    )

    live = await container.deployer.list_live()
    assert len(live) == 1
    assert live[0].url.endswith(str(allocations[0].port))


async def test_two_runs_never_share_a_port(container: Container, worker: Worker) -> None:
    ports: list[int] = []
    for _ in range(3):
        run = await container.runs.create("a brief")
        await drain(worker, container)
        await container.runs.decide(run.id, Gate.SPEC, approved=True, decided_by="yash")
        await drain(worker, container)
        await container.runs.decide(run.id, Gate.DEPLOY, approved=True, decided_by="yash")
        await drain(worker, container)

    async with container.uow() as uow:
        ports = [a.port for a in await uow.ports.list_active(host="127.0.0.1")]

    assert len(ports) == 3
    assert len(set(ports)) == 3


async def test_the_reconciler_is_clean_after_a_completed_run(
    container: Container, worker: Worker
) -> None:
    run = await container.runs.create("a brief")
    await drain(worker, container)
    await container.runs.decide(run.id, Gate.SPEC, approved=True, decided_by="yash")
    await drain(worker, container)
    await container.runs.decide(run.id, Gate.DEPLOY, approved=True, decided_by="yash")
    await drain(worker, container)

    report = await container.reconciler.inspect()

    # A successfully deployed run is terminal, but its deployment and its port
    # are exactly what the run was for. Reporting them as orphans would have
    # `reconcile --apply` tear down a live client application.
    assert report.is_clean, [(d.kind, d.subject, d.detail) for d in report.all]


async def test_a_cancelled_run_leaves_its_deployment_reportable(
    container: Container, worker: Worker
) -> None:
    """The other half of the rule: a run that did not survive owns nothing.

    Arranged the way it actually happens -- a run is cancelled while its deploy
    is in flight, after the port and the deployment id have been recorded but
    before the run reached DEPLOYED.
    """
    from app.deploy.base import DeploymentTarget
    from app.domain.entities import Trigger
    from app.domain.ids import uuid7

    live = await container.runs.create("stays up")
    await drain(worker, container)
    await container.runs.decide(live.id, Gate.SPEC, approved=True, decided_by="yash")
    await drain(worker, container)
    await container.runs.decide(live.id, Gate.DEPLOY, approved=True, decided_by="yash")
    await drain(worker, container)
    assert (await container.runs.get(live.id)).state is RunState.DEPLOYED

    doomed = await container.runs.create("gets cancelled mid-deploy")
    await drain(worker, container)
    await container.runs.decide(doomed.id, Gate.SPEC, approved=True, decided_by="yash")
    await drain(worker, container)
    await container.runs.decide(doomed.id, Gate.DEPLOY, approved=True, decided_by="yash")

    # The deploy job gets as far as recording its resources, then the run is
    # cancelled before the job finishes.
    allocation = await container.ports.allocate(doomed.id, host="127.0.0.1")
    deployment_id = uuid7()
    await container.deployer.deploy(
        DeploymentTarget(
            run_id=doomed.id,
            deployment_id=deployment_id,
            host="127.0.0.1",
            project_name=f"slipway-{deployment_id}",
            port=allocation.port,
        ),
        "memory://artifact",
        timeout_seconds=5.0,
    )
    await container.runs.advance(
        doomed.id, Trigger.CANCELLED, actor="yash", detail="client pulled it"
    )

    report = await container.reconciler.inspect()
    reported = {(d.kind, d.run_id) for d in report.all}

    assert ("port", doomed.id) in reported
    assert ("deployment", doomed.id) in reported
    assert ("port", live.id) not in reported, "the live deployment must be left alone"
    assert ("deployment", live.id) not in reported
