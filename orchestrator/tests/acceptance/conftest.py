"""The full loop, against a real Postgres and the fake seams.

Fake seams, not fake logic: the worker, the services, the state machine, the
job queue and the database are all the real ones. What is substituted is the
model provider, the sandbox, the deploy target and the artifact store -- the
four things that would otherwise cost money or need a server.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from app.config import Settings
from app.container import Container, build_container
from tests.integration.conftest import (  # noqa: F401  -- fixtures are used by name
    _database_url,
    clean_database,
    engine,
)

PROMPTS = Path(__file__).resolve().parents[3] / "prompts"


@pytest.fixture(scope="session")
def settings() -> Settings:
    url = _database_url()
    if not url:
        pytest.skip("set SLIPWAY_DATABASE_URL (or run `make test-acceptance`)")
    return Settings(
        database_url=url,
        models_backend="fake",
        runtime_backend="langgraph_local",
        sandbox_backend="fake",
        deploy_backend="fake",
        artifacts_backend="memory",
        deploy_public_host="127.0.0.1",
        job_lease_seconds=60,
        worker_poll_seconds=0.01,
        job_max_attempts=2,
        log_json=True,
    )


@pytest.fixture
async def container(settings: Settings, clean_database: None) -> AsyncIterator[Container]:  # noqa: F811
    built = build_container(settings, prompts_root=PROMPTS)
    yield built
    await built.aclose()


@pytest.fixture
def worker(container: Container) -> Iterator[object]:
    from app.worker import Worker

    yield Worker(container)
