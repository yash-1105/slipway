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
import yaml

from app.config import Settings
from app.container import Container, build_container
from tests.integration.conftest import (  # noqa: F401  -- fixtures are used by name
    _database_url,
    clean_database,
    engine,
)

PROMPTS = Path(__file__).resolve().parents[3] / "prompts"


#: A routing table for the fake backend. The ids and prices are obviously not
#: real -- nothing here is a provider model id -- but they are explicit, so the
#: ledger rows these tests produce have numbers that can be checked by hand.
FAKE_ROUTING: dict[str, object] = {
    "source_base_url": "fake://models",
    "synced_at": "2026-01-01T00:00:00+00:00",
    "roles": {
        name: {
            "intent": f"whatever {name} is meant to use",
            "context_window": 128000,
            "max_output_tokens": 8192,
            "primary": {
                "model_id": f"fake/{name}-primary",
                "input_usd_per_mtok": 2,
                "output_usd_per_mtok": 10,
            },
            "fallback": {
                "model_id": f"fake/{name}-fallback",
                "input_usd_per_mtok": 1,
                "output_usd_per_mtok": 4,
            },
        }
        for name in ("planner", "builder", "evaluator", "test_author", "doc_writer")
    },
    "available": [],
}

#: The rate these tests convert at. A round number so an INR figure in a
#: failure message is legible.
TEST_USD_TO_INR = 80.0


@pytest.fixture(scope="session")
def models_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("models") / "models.yaml"
    path.write_text(yaml.safe_dump(FAKE_ROUTING))
    return path


@pytest.fixture(scope="session")
def settings(models_file: Path) -> Settings:
    url = _database_url()
    if not url:
        pytest.skip("set SLIPWAY_DATABASE_URL (or run `make test-acceptance`)")
    return Settings(
        models_catalogue_path=models_file,
        usd_to_inr=TEST_USD_TO_INR,
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
