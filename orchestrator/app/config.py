"""The only module in Slipway that reads the environment.

Everything else takes a `Settings` instance. This is enforced by an
import-linter contract, not by convention.

Configuration is validated here and the process refuses to boot if it is
wrong -- a missing Novita key surfaces as a startup failure with a sentence
saying which variable is missing, not as a 401 during the first agent call.
"""

from __future__ import annotations

import functools
import os
import socket
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.errors import ConfigError

#: Repository root, derived from this file's location. Used only to build
#: defaults for paths, so that nothing in the codebase contains an absolute
#: path and a checkout works from wherever it is cloned.
REPO_ROOT = Path(__file__).resolve().parents[2]

ModelsBackend = Literal["novita", "fake"]
RuntimeBackend = Literal["langgraph_local", "fake"]
SandboxBackend = Literal["docker", "fake"]
DeployBackend = Literal["local_container", "fake"]
ArtifactsBackend = Literal["local_fs", "memory"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SLIPWAY_",
        env_file=None,          # secrets come from the environment, never a file
        extra="forbid",
        frozen=True,
    )

    # --- database ----------------------------------------------------------
    database_url: str = Field(
        description="postgresql+asyncpg://... -- required, no default, deliberately"
    )
    database_pool_size: int = 10
    database_connect_timeout_seconds: float = 10.0
    database_statement_timeout_seconds: float = 30.0

    # --- API ---------------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_cors_origins: tuple[str, ...] = ()

    # --- seam selectors (ADR 0001) -----------------------------------------
    models_backend: ModelsBackend = "novita"
    runtime_backend: RuntimeBackend = "langgraph_local"
    sandbox_backend: SandboxBackend = "docker"
    deploy_backend: DeployBackend = "local_container"
    artifacts_backend: ArtifactsBackend = "local_fs"

    # --- models seam -------------------------------------------------------
    novita_api_key: str = ""
    novita_base_url: str = "https://api.novita.ai/openai/v1"
    models_catalogue_path: Path = REPO_ROOT / "config" / "models.yaml"
    model_timeout_seconds: float = 120.0
    model_max_attempts: int = 3

    # --- sandbox seam ------------------------------------------------------
    docker_binary: str = "docker"
    sandbox_image: str = "slipway/sandbox:dev"
    sandbox_name_prefix: str = "slipway-sbx-"
    sandbox_workspace_root: Path = REPO_ROOT / ".slipway" / "workspaces"
    sandbox_cpu_limit: float = 2.0
    sandbox_memory_limit_mb: int = 2048
    sandbox_exec_timeout_seconds: float = 900.0

    # --- deploy seam -------------------------------------------------------
    #: The host that appears in a preview URL. Containers publish on loopback,
    #: so this is 127.0.0.1 on a laptop and the tunnel's hostname when a client
    #: is being shown something.
    deploy_public_host: str = "127.0.0.1"
    deploy_timeout_seconds: float = 600.0
    #: How long to wait for a container's own HEALTHCHECK to pass.
    deploy_health_timeout_seconds: float = 120.0
    deploy_port_range_start: int = 41000
    deploy_port_range_end: int = 41999

    # --- artifacts seam ----------------------------------------------------
    artifacts_root: Path = REPO_ROOT / ".slipway" / "artifacts"
    artifact_max_bytes: int = 64 * 1024 * 1024
    artifact_timeout_seconds: float = 60.0

    # --- worker ------------------------------------------------------------
    worker_id: str = ""
    worker_poll_seconds: float = 2.0
    job_lease_seconds: int = 300
    job_max_attempts: int = 5
    worker_kinds: tuple[str, ...] = ("specify", "build", "test", "deploy", "reconcile")

    # --- cost ledger --------------------------------------------------------
    #: Rate every ledger row is converted at, stored per row so a historical
    #: total does not silently change when the rate moves. No default that
    #: looks like a real rate: an invented number here becomes an invented
    #: number in the accounts.
    usd_to_inr: float = 0.0

    # --- budget ------------------------------------------------------------
    budget_daily_usd_cap: float = 25.0
    budget_run_usd_cap: float = 5.0

    # --- integrations ------------------------------------------------------
    github_token: str = ""
    github_org: str = ""

    # --- logging -----------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        problems: list[str] = []

        if not self.database_url.startswith("postgresql+asyncpg://"):
            problems.append(
                "SLIPWAY_DATABASE_URL must start with postgresql+asyncpg:// "
                f"(got {self.database_url.split('://')[0]!r}://...)"
            )

        if self.models_backend == "novita" and not self.novita_api_key:
            problems.append(
                "SLIPWAY_NOVITA_API_KEY is required when SLIPWAY_MODELS_BACKEND=novita"
            )

        if self.models_backend == "novita" and self.usd_to_inr <= 0:
            problems.append(
                "SLIPWAY_USD_TO_INR must be set to a positive rate when "
                "SLIPWAY_MODELS_BACKEND=novita; every ledger row records the "
                "rate it was converted at and there is no sensible default"
            )

        if self.deploy_backend == "local_container" and not self.deploy_public_host:
            problems.append(
                "SLIPWAY_DEPLOY_PUBLIC_HOST is required when "
                "SLIPWAY_DEPLOY_BACKEND=local_container; it is the host that "
                "appears in the URL handed to a reviewer"
            )

        if self.deploy_port_range_start >= self.deploy_port_range_end:
            problems.append(
                "SLIPWAY_DEPLOY_PORT_RANGE_START must be below "
                "SLIPWAY_DEPLOY_PORT_RANGE_END"
            )

        if self.model_max_attempts < 1:
            problems.append("SLIPWAY_MODEL_MAX_ATTEMPTS must be at least 1")

        if self.job_lease_seconds <= self.worker_poll_seconds:
            problems.append(
                "SLIPWAY_JOB_LEASE_SECONDS must exceed SLIPWAY_WORKER_POLL_SECONDS, "
                "or a worker will lose its own lease between polls"
            )

        if self.budget_run_usd_cap > self.budget_daily_usd_cap:
            problems.append(
                "SLIPWAY_BUDGET_RUN_USD_CAP cannot exceed SLIPWAY_BUDGET_DAILY_USD_CAP"
            )

        if problems:
            raise ConfigError(
                "configuration is invalid, refusing to start:\n  - " + "\n  - ".join(problems)
            )

        return self

    @property
    def effective_worker_id(self) -> str:
        """A worker's lease holder name. Stable per process, unique per host."""
        return self.worker_id or f"{socket.gethostname()}:{os.getpid()}"


def _unknown_env_vars() -> list[str]:
    """SLIPWAY_-prefixed variables that match no field.

    pydantic-settings ignores these rather than rejecting them, so without this
    check a typo -- SLIPWAY_NOVITA_API_KEYS, SLIPWAY_JOB_LEASE_SECOND -- is
    silently a no-op and the process boots with a default the operator thinks
    they overrode. `extra="forbid"` does not cover it: unknown env vars never
    reach the model as extra fields in the first place.
    """
    known = {f"SLIPWAY_{name.upper()}" for name in Settings.model_fields}
    return sorted(
        name for name in os.environ if name.startswith("SLIPWAY_") and name not in known
    )


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and validate settings once per process.

    Raises ConfigError with every problem listed, rather than the first one, so
    a misconfigured checkout takes one round trip to fix instead of five.
    """
    unknown = _unknown_env_vars()
    if unknown:
        raise ConfigError(
            "configuration is invalid, refusing to start:\n"
            "  - these SLIPWAY_ variables match no setting and would be ignored:\n    "
            + "\n    ".join(unknown)
        )

    try:
        return Settings()
    except ConfigError:
        raise
    except ValueError as exc:
        # pydantic's ValidationError is a ValueError; translate it so callers
        # only ever have to catch ConfigError at startup.
        raise ConfigError(f"configuration is invalid, refusing to start:\n{exc}") from exc
