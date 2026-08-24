"""The composition root.

The single place that knows both which concrete implementations exist and how
they fit together. It selects seams through their factories -- never by
importing an implementation -- and hands finished services to the adapters.

api/, cli/, main.py and worker.py take what they need from here. Nothing else
constructs a service.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.graphs import GraphRegistry, build_registry
from app.agents.prompts import PromptLibrary
from app.artifacts.base import ArtifactStore
from app.artifacts.factory import build_artifact_store
from app.config import REPO_ROOT, Settings
from app.db.session import build_engine, build_session_factory
from app.db.uow import SqlUnitOfWork
from app.deploy.base import Deployer
from app.deploy.factory import build_deployer
from app.domain.repositories import UnitOfWork
from app.models.base import ModelCatalogue, ModelClient
from app.models.factory import build_model_client
from app.models.router import CostCollector, RoutingTable, catalogue_for, load_routing
from app.runtimes.base import GraphRuntime
from app.runtimes.factory import build_runtime
from app.sandbox.base import Sandbox
from app.sandbox.factory import build_sandbox
from app.services.deploys import DeployService
from app.services.health import HealthService
from app.services.jobs import JobService
from app.services.migrations import MigrationService
from app.services.models import ModelsService
from app.services.reconcile import Reconciler
from app.services.runs import RunService


@dataclass(frozen=True)
class Container:
    settings: Settings
    engine: AsyncEngine
    uow: Callable[[], UnitOfWork]
    catalogue: ModelCatalogue
    routing: RoutingTable
    models: ModelClient
    costs: CostCollector
    sandbox: Sandbox
    deployer: Deployer
    artifacts: ArtifactStore
    graphs: GraphRegistry
    runtime: GraphRuntime
    runs: RunService
    health: HealthService
    migrations: MigrationService
    models_service: ModelsService
    jobs: JobService
    reconciler: Reconciler
    deploys: DeployService

    async def aclose(self) -> None:
        await self.engine.dispose()


def build_container(settings: Settings, *, prompts_root: Path | None = None) -> Container:
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)

    def uow_factory() -> UnitOfWork:
        return SqlUnitOfWork(session_factory)

    routing = load_routing(
        settings.models_catalogue_path,
        require_models=settings.models_backend != "fake",
    )
    catalogue = catalogue_for(routing, "primary")
    # One collector, shared by the router that fills it and the worker that
    # drains it. Nothing else touches it.
    costs = CostCollector()
    models = build_model_client(settings, routing, costs)
    sandbox = build_sandbox(settings)
    deployer = build_deployer(settings)
    artifacts = build_artifact_store(settings)

    prompts = PromptLibrary(prompts_root or REPO_ROOT / "prompts")
    graphs = build_registry(
        models=models, prompts=prompts, timeout_seconds=settings.model_timeout_seconds
    )
    runtime = build_runtime(settings, graphs)

    return Container(
        settings=settings,
        engine=engine,
        uow=uow_factory,
        catalogue=catalogue,
        routing=routing,
        models=models,
        costs=costs,
        sandbox=sandbox,
        deployer=deployer,
        artifacts=artifacts,
        graphs=graphs,
        runtime=runtime,
        runs=RunService(uow_factory),
        health=HealthService(
            engine, catalogue, timeout_seconds=settings.database_connect_timeout_seconds
        ),
        jobs=JobService(
            uow_factory,
            worker_id=settings.effective_worker_id,
            lease_seconds=settings.job_lease_seconds,
            max_attempts=settings.job_max_attempts,
        ),
        models_service=ModelsService(
            models, catalogue, timeout_seconds=settings.model_timeout_seconds
        ),
        migrations=MigrationService(engine, REPO_ROOT / "orchestrator" / "migrations"),
        deploys=DeployService(
            uow_factory,
            deployer,
            host=settings.deploy_public_host,
            port_range=(settings.deploy_port_range_start, settings.deploy_port_range_end),
            timeout_seconds=settings.deploy_timeout_seconds,
        ),
        reconciler=Reconciler(
            uow_factory,
            sandbox,
            deployer,
            deploy_host=settings.deploy_public_host or settings.api_host,
            teardown_timeout_seconds=settings.deploy_timeout_seconds,
        ),
    )
