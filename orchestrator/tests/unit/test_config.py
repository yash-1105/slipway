"""Configuration validation. The process must refuse to boot when it is wrong."""

from __future__ import annotations

import pytest

from app.config import Settings, get_settings
from app.domain.errors import ConfigError

GOOD = {
    "database_url": "postgresql+asyncpg://slipway:secret@localhost:5432/slipway",
    "models_backend": "fake",
    "sandbox_backend": "fake",
    "deploy_backend": "fake",
    "artifacts_backend": "memory",
}


def settings(**overrides: object) -> Settings:
    return Settings(**{**GOOD, **overrides})  # type: ignore[arg-type]


def _env(**overrides: str) -> dict[str, str]:
    """The GOOD settings as the environment variables they really come from."""
    fields = {**{k: str(v) for k, v in GOOD.items()}, **overrides}
    return {f"SLIPWAY_{name.upper()}": value for name, value in fields.items()}


def test_a_valid_configuration_loads() -> None:
    assert settings().models_backend == "fake"


def test_a_non_asyncpg_database_url_is_refused() -> None:
    """psycopg2 URLs load fine and then deadlock the event loop at runtime."""
    with pytest.raises(ConfigError, match="postgresql\\+asyncpg"):
        settings(database_url="postgresql://slipway@localhost/slipway")


def test_novita_backend_without_a_key_is_refused() -> None:
    with pytest.raises(ConfigError, match="SLIPWAY_NOVITA_API_KEY"):
        settings(models_backend="novita", novita_api_key="")


def test_local_container_deploy_without_a_public_host_is_refused() -> None:
    """The public host is what appears in the URL handed to a reviewer."""
    with pytest.raises(ConfigError, match="SLIPWAY_DEPLOY_PUBLIC_HOST"):
        settings(deploy_backend="local_container", deploy_public_host="")


def test_every_problem_is_reported_at_once() -> None:
    """One round trip to fix a broken checkout, not five."""
    with pytest.raises(ConfigError) as caught:
        settings(
            database_url="mysql://nope",
            models_backend="novita",
            novita_api_key="",
            deploy_backend="local_container",
            deploy_public_host="",
        )
    message = str(caught.value)
    assert "postgresql+asyncpg" in message
    assert "SLIPWAY_NOVITA_API_KEY" in message
    assert "SLIPWAY_DEPLOY_PUBLIC_HOST" in message


def test_a_lease_shorter_than_the_poll_interval_is_refused() -> None:
    """Otherwise a worker loses its own lease between polls and jobs thrash."""
    with pytest.raises(ConfigError, match="JOB_LEASE_SECONDS"):
        settings(job_lease_seconds=1, worker_poll_seconds=5.0)


def test_a_run_cap_above_the_daily_cap_is_refused() -> None:
    with pytest.raises(ConfigError, match="BUDGET_RUN_USD_CAP"):
        settings(budget_run_usd_cap=100.0, budget_daily_usd_cap=25.0)


def test_an_inverted_port_range_is_refused() -> None:
    with pytest.raises(ConfigError, match="PORT_RANGE_START"):
        settings(deploy_port_range_start=42000, deploy_port_range_end=41000)


def test_an_unknown_setting_is_refused_rather_than_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo in an env var must not silently do nothing.

    Goes through get_settings() rather than Settings() directly, because that is
    the real boot path and the one that turns pydantic's ValidationError into a
    ConfigError an operator can read.
    """
    for name, value in _env(sandbox_memory_limit_mbb="4096").items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    with pytest.raises(ConfigError, match="refusing to start"):
        get_settings()

    get_settings.cache_clear()


def test_get_settings_reports_a_missing_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(_env().keys()):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()

    with pytest.raises(ConfigError, match="refusing to start"):
        get_settings()

    get_settings.cache_clear()


def test_settings_are_frozen() -> None:
    """Nothing reconfigures itself at runtime."""
    config = settings()
    with pytest.raises(ValueError, match=r"frozen|immutable"):
        config.api_port = 9999  # type: ignore[misc]


def test_the_worker_id_defaults_to_something_unique_per_process() -> None:
    import os

    assert settings().effective_worker_id.endswith(f":{os.getpid()}")
    assert settings(worker_id="ci-runner").effective_worker_id == "ci-runner"
