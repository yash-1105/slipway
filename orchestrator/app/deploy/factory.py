"""deploy seam factory. Selector: SLIPWAY_DEPLOY_BACKEND."""

from __future__ import annotations

from app.config import Settings
from app.deploy.base import Deployer
from app.domain.errors import ConfigError


def build_deployer(settings: Settings) -> Deployer:
    backend = settings.deploy_backend
    if backend == "local_container":
        from app.deploy.impl.local_container import LocalContainerDeployer

        return LocalContainerDeployer(
            docker_binary=settings.docker_binary,
            public_host=settings.deploy_public_host,
            network=settings.deploy_network,
            health_timeout_seconds=settings.deploy_health_timeout_seconds,
        )
    if backend == "fake":
        from app.deploy.impl.fake import FakeDeployer

        return FakeDeployer()
    raise ConfigError(f"unknown SLIPWAY_DEPLOY_BACKEND: {backend!r}")
