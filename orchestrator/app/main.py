"""The FastAPI application.

An entry point and a composition root, nothing else. It builds the container,
attaches it to app.state and mounts the routers.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import install_error_handlers
from app.api.routes_health import router as health_router
from app.api.routes_runs import router as runs_router
from app.config import Settings, get_settings
from app.container import build_container
from app.logging import configure_logging

log = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    configure_logging(resolved)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = build_container(resolved)
        app.state.container = container
        log.info(
            "api.started",
            models_backend=resolved.models_backend,
            sandbox_backend=resolved.sandbox_backend,
            deploy_backend=resolved.deploy_backend,
            artifacts_backend=resolved.artifacts_backend,
            catalogue_synced_at=container.catalogue.synced_at,
        )
        try:
            yield
        finally:
            await container.aclose()

    app = FastAPI(title="Slipway", version="0.1.0", lifespan=lifespan)

    if resolved.api_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.api_cors_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(runs_router)
    return app


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,   # structlog owns logging
    )


if __name__ == "__main__":
    main()
