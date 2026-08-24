"""A brief goes in, a deployed run comes out, pausing at both human gates."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from app.artifacts.base import StoredArtifact
from app.container import Container
from app.deploy.base import Deployment
from app.domain.entities import ArtifactRef, Gate, RunState
from app.domain.ids import uuid7
from app.worker import DEPLOYABLE_ARTIFACT_KIND, Worker

pytestmark = pytest.mark.acceptance


async def stage_bundle(container: Container, run_id: UUID) -> str:
    """Put a deployable bundle where the deploy stage will look for it.

    Nothing in the pipeline produces one yet: the build stage emits an agent's
    prose about a build, not a build. A test that wants to exercise deployment
    therefore has to stage the artifact itself, and say so.

    The previous version of these tests did not, and passed anyway, because the
    deploy job fell back to the build log and the fake deployer accepted any
    URI. They asserted a success that could not happen against a real deployer.
    """
    stored = await container.artifacts.put(
        run_id, DEPLOYABLE_ARTIFACT_KIND, b"a tarball would go here", timeout_seconds=5.0
    )
    assert isinstance(stored, StoredArtifact)

    async with container.uow() as uow:
        await uow.artifacts.record(
            ArtifactRef(
                id=uuid7(),
                run_id=run_id,
                kind=DEPLOYABLE_ARTIFACT_KIND,
                uri=stored.uri,
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                created_at=datetime.now(UTC),
            )
        )
        await uow.commit()
    return stored.uri


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

    await stage_bundle(container, run.id)
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
    await stage_bundle(container, run.id)
    await container.runs.decide(run.id, Gate.DEPLOY, approved=True, decided_by="yash")
    await drain(worker, container)

    # The port now lives on the deployment record itself, held by the partial
    # unique index while destroyed_at is NULL.
    async with container.uow() as uow:
        records = await uow.deployments.list_holding_ports(host="127.0.0.1")

    assert len(records) == 1
    assert records[0].run_id == run.id
    assert records[0].container_name, "the container name is recorded before it exists"
    assert (
        container.settings.deploy_port_range_start
        <= records[0].port
        <= container.settings.deploy_port_range_end
    )

    live = await container.deployer.list_live()
    assert len(live) == 1
    assert live[0].url.endswith(str(records[0].port))


async def test_two_runs_never_share_a_port(container: Container, worker: Worker) -> None:
    ports: list[int] = []
    for _ in range(3):
        run = await container.runs.create("a brief")
        await drain(worker, container)
        await container.runs.decide(run.id, Gate.SPEC, approved=True, decided_by="yash")
        await drain(worker, container)
        await stage_bundle(container, run.id)
        await container.runs.decide(run.id, Gate.DEPLOY, approved=True, decided_by="yash")
        await drain(worker, container)

    async with container.uow() as uow:
        ports = [d.port for d in await uow.deployments.list_holding_ports(host="127.0.0.1")]

    assert len(ports) == 3
    assert len(set(ports)) == 3


async def test_the_reconciler_is_clean_after_a_completed_run(
    container: Container, worker: Worker
) -> None:
    run = await container.runs.create("a brief")
    await drain(worker, container)
    await container.runs.decide(run.id, Gate.SPEC, approved=True, decided_by="yash")
    await drain(worker, container)
    await stage_bundle(container, run.id)
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
    is in flight, after the deployment row and its port have been recorded but
    before the run reached DEPLOYED.

    Ported from the standalone port allocator to DeployService, which now holds
    the port on the deployment row. The properties are the same four: the
    cancelled run's deployment is reported, its port is held, and neither is
    true of the live one.
    """
    from app.domain.entities import Trigger

    live = await container.runs.create("stays up")
    await drain(worker, container)
    await container.runs.decide(live.id, Gate.SPEC, approved=True, decided_by="yash")
    await drain(worker, container)
    await stage_bundle(container, live.id)
    await container.runs.decide(live.id, Gate.DEPLOY, approved=True, decided_by="yash")
    await drain(worker, container)
    assert (await container.runs.get(live.id)).state is RunState.DEPLOYED

    doomed = await container.runs.create("gets cancelled mid-deploy")
    await drain(worker, container)
    await container.runs.decide(doomed.id, Gate.SPEC, approved=True, decided_by="yash")
    await drain(worker, container)
    await stage_bundle(container, doomed.id)
    await container.runs.decide(doomed.id, Gate.DEPLOY, approved=True, decided_by="yash")

    # The deploy gets as far as recording its resources and starting, then the
    # run is cancelled before the job finishes.
    doomed_deploy = await container.deploys.deploy(
        doomed.id, context_path="", artifact_uri="memory://artifact"
    )
    assert isinstance(doomed_deploy, Deployment), getattr(doomed_deploy, "detail", "")
    await container.runs.advance(
        doomed.id, Trigger.CANCELLED, actor="yash", detail="client pulled it"
    )

    # Both runs hold a port at this point; one is entitled to and one is not.
    async with container.uow() as uow:
        holders = {d.run_id for d in
                   await uow.deployments.list_holding_ports(host="127.0.0.1")}
    assert doomed.id in holders, "the cancelled run's deployment still holds its port"
    assert live.id in holders, "the live run's deployment still holds its port"

    report = await container.reconciler.inspect()
    reported = {(d.kind, d.run_id) for d in report.all}

    assert ("deployment", doomed.id) in reported
    assert ("deployment", live.id) not in reported, "the live deployment must be left alone"

    # The repair is asymmetric: it frees the cancelled run's port and leaves the
    # live one exactly as it was.
    await container.reconciler.apply(report)

    doomed_record = await container.deploys.get(doomed_deploy.deployment_id)
    assert doomed_record is not None
    assert doomed_record.destroyed_at is not None, "the cancelled run's port must be released"

    async with container.uow() as uow:
        still_holding = {d.run_id for d in
                         await uow.deployments.list_holding_ports(host="127.0.0.1")}
    assert doomed.id not in still_holding
    assert live.id in still_holding, "apply() released a port that is still in use"


async def test_a_deploy_with_no_bundle_fails_by_name(
    container: Container, worker: Worker
) -> None:
    """The deploy stage refuses rather than deploying whatever is lying around.

    This is what every run does today, because no stage produces a bundle. The
    previous behaviour was to fall back to the build log -- an agent's prose
    about a build -- and hand it to the deployer, which with a real deployer
    means a URL that serves nothing and a run marked DEPLOYED.
    """
    run = await container.runs.create("a brief")
    await drain(worker, container)
    await container.runs.decide(run.id, Gate.SPEC, approved=True, decided_by="yash")
    await drain(worker, container)

    # Note: no stage_bundle() call. This is the honest state of the pipeline.
    run = await container.runs.decide(run.id, Gate.DEPLOY, approved=True, decided_by="yash")
    assert run.state is RunState.DEPLOYING

    await drain(worker, container)

    run = await container.runs.get(run.id)
    assert run.state is RunState.FAILED
    assert run.failure_reason is not None
    assert "no deployable artifact" in run.failure_reason
    assert DEPLOYABLE_ARTIFACT_KIND in run.failure_reason


async def test_a_refused_deploy_allocates_nothing(
    container: Container, worker: Worker
) -> None:
    """Refusing early means no port is taken and no deployment is created.

    The check runs before the allocator, so a run that cannot deploy does not
    consume a port from a bounded range on its way to failing.
    """
    run = await container.runs.create("a brief")
    await drain(worker, container)
    await container.runs.decide(run.id, Gate.SPEC, approved=True, decided_by="yash")
    await drain(worker, container)
    await container.runs.decide(run.id, Gate.DEPLOY, approved=True, decided_by="yash")
    await drain(worker, container)

    async with container.uow() as uow:
        holders = await uow.deployments.list_holding_ports(host="127.0.0.1")
        for_this_run = await uow.deployments.list_for_run(run.id)

    # Asserting on `deployments`, not the retired port_allocations table. The
    # old assertion read a table nothing wrote any more, so it passed however
    # much a refused deploy allocated -- verified by mutation.
    assert holders == [], f"a refused deploy is holding ports: {holders}"
    assert for_this_run == [], "a refused deploy created a deployment record"
    assert await container.deployer.list_live() == []


async def test_the_refusal_is_not_retried(container: Container, worker: Worker) -> None:
    """A missing artifact cannot be retried into existence.

    Burning the attempt cap on it delays the diagnosis and buries the real
    reason under 'attempts exhausted'.
    """
    run = await container.runs.create("a brief")
    await drain(worker, container)
    await container.runs.decide(run.id, Gate.SPEC, approved=True, decided_by="yash")
    await drain(worker, container)
    await container.runs.decide(run.id, Gate.DEPLOY, approved=True, decided_by="yash")
    await drain(worker, container)

    async with container.uow() as uow:
        events = await uow.events.list_for_run(run.id)

    abandoned = [e for e in events if e.kind == "job.abandoned"]
    assert len(abandoned) == 1
    assert abandoned[0].payload["kind"] == "deploy"
    assert abandoned[0].payload["attempts"] == 1, "abandoned on the first attempt"

    # And the job really is terminal, not waiting to be picked up again.
    assert await container.jobs.claim(frozenset({"deploy"})) is None
    async with container.uow() as uow:
        assert await uow.jobs.list_expired(now=datetime.now(UTC)) == []


async def test_every_model_call_lands_in_the_ledger(
    container: Container, worker: Worker
) -> None:
    """Cost is recorded per call, attributed to the run, with the acting user."""
    run = await container.runs.create("a brief")
    await drain(worker, container)  # the planner stage

    async with container.uow() as uow:
        entries = await uow.costs.list_for_run(run.id)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.role == "planner"
    assert entry.model_used == "fake/planner-primary"
    assert entry.model_requested == "fake/planner-primary"
    assert entry.prompt_tokens > 0
    assert entry.actor.startswith("worker:")
    assert entry.usd > 0
    assert entry.inr == entry.usd * entry.usd_to_inr
    assert entry.priced is True


async def test_the_ledger_records_the_rate_it_converted_at(
    container: Container, worker: Worker
) -> None:
    """A historical total must not change when the rate moves."""
    from tests.acceptance.conftest import TEST_USD_TO_INR

    run = await container.runs.create("a brief")
    await drain(worker, container)

    async with container.uow() as uow:
        (entry,) = await uow.costs.list_for_run(run.id)

    assert entry.usd_to_inr == Decimal(str(TEST_USD_TO_INR))


async def test_every_stage_of_a_run_is_costed_against_that_run(
    container: Container, worker: Worker
) -> None:
    run = await container.runs.create("a brief")
    await drain(worker, container)
    await container.runs.decide(run.id, Gate.SPEC, approved=True, decided_by="yash")
    await drain(worker, container)

    async with container.uow() as uow:
        entries = await uow.costs.list_for_run(run.id)

    assert [e.role for e in entries] == ["planner", "builder", "test_author"]
    assert all(e.run_id == run.id for e in entries)
    assert all(e.job_id is not None for e in entries)


async def test_two_runs_costs_do_not_mix(container: Container, worker: Worker) -> None:
    first = await container.runs.create("first brief")
    second = await container.runs.create("second brief")
    await drain(worker, container)

    async with container.uow() as uow:
        first_entries = await uow.costs.list_for_run(first.id)
        second_entries = await uow.costs.list_for_run(second.id)

    assert len(first_entries) == 1
    assert len(second_entries) == 1
    assert first_entries[0].run_id == first.id
    assert second_entries[0].run_id == second.id


async def test_nothing_is_left_pending_in_the_collector(
    container: Container, worker: Worker
) -> None:
    """Anything undrained is cost that never reaches the ledger."""
    run = await container.runs.create("a brief")
    await drain(worker, container)
    await container.runs.decide(run.id, Gate.SPEC, approved=True, decided_by="yash")
    await drain(worker, container)

    assert container.costs.pending() == 0
