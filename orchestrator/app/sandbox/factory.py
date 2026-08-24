"""sandbox seam factory. Selector: SLIPWAY_SANDBOX_BACKEND."""

from __future__ import annotations

from app.config import Settings
from app.domain.errors import ConfigError
from app.sandbox.base import Sandbox


def build_sandbox(settings: Settings) -> Sandbox:
    backend = settings.sandbox_backend
    if backend == "docker":
        from app.sandbox.impl.docker import DockerSandbox

        return DockerSandbox(
            docker_binary=settings.docker_binary,
            name_prefix=settings.sandbox_name_prefix,
        )
    if backend == "fake":
        from app.sandbox.impl.fake import FakeSandbox

        return FakeSandbox()
    raise ConfigError(f"unknown SLIPWAY_SANDBOX_BACKEND: {backend!r}")
