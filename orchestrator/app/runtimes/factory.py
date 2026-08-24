"""runtimes seam factory. Selector: SLIPWAY_RUNTIME_BACKEND."""

from __future__ import annotations

from app.config import Settings
from app.domain.errors import ConfigError
from app.runtimes.base import GraphRuntime


def build_runtime(settings: Settings, graphs: object) -> GraphRuntime:
    """`graphs` is the registry from app/agents; it is passed in rather than
    imported here so the seam does not depend on the agent layer."""
    backend = settings.runtime_backend
    if backend == "langgraph_local":
        from app.runtimes.impl.langgraph_local import LocalLangGraphRuntime

        return LocalLangGraphRuntime(graphs=graphs)
    if backend == "fake":
        from app.runtimes.impl.fake import FakeRuntime

        return FakeRuntime()
    raise ConfigError(f"unknown SLIPWAY_RUNTIME_BACKEND: {backend!r}")
