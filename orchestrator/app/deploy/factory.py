"""deploy seam factory. Selector: SLIPWAY_DEPLOY_BACKEND."""

from __future__ import annotations

from app.config import Settings
from app.deploy.base import Deployer
from app.domain.errors import ConfigError


def build_deployer(settings: Settings) -> Deployer:
    backend = settings.deploy_backend
    if backend == "compose_ssh":
        from app.deploy.impl.compose_ssh import ComposeOverSshDeployer

        return ComposeOverSshDeployer(
            ssh_host=settings.deploy_ssh_host,
            ssh_user=settings.deploy_ssh_user,
            remote_root=settings.deploy_remote_root,
            public_host=settings.deploy_public_host,
            ssh_binary=settings.ssh_binary,
        )
    if backend == "fake":
        from app.deploy.impl.fake import FakeDeployer

        return FakeDeployer()
    raise ConfigError(f"unknown SLIPWAY_DEPLOY_BACKEND: {backend!r}")
