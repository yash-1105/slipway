"""artifacts seam factory. Selector: SLIPWAY_ARTIFACTS_BACKEND."""

from __future__ import annotations

from app.artifacts.base import ArtifactStore
from app.config import Settings
from app.domain.errors import ConfigError


def build_artifact_store(settings: Settings) -> ArtifactStore:
    backend = settings.artifacts_backend
    if backend == "local_fs":
        from app.artifacts.impl.local_fs import LocalFilesystemArtifactStore

        return LocalFilesystemArtifactStore(
            root=settings.artifacts_root,
            max_bytes=settings.artifact_max_bytes,
        )
    if backend == "memory":
        from app.artifacts.impl.memory import InMemoryArtifactStore

        return InMemoryArtifactStore(max_bytes=settings.artifact_max_bytes)
    raise ConfigError(f"unknown SLIPWAY_ARTIFACTS_BACKEND: {backend!r}")
