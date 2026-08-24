"""models seam factory.

Reads one selector, SLIPWAY_MODELS_BACKEND, through app/config.py. Concrete
implementations are imported inside the function so that selecting one does not
import the others, and so nothing above the seam can reach them.

Both backends are wrapped in the router, so role resolution, fallback and cost
recording behave the same in a test as in production. The only thing the
selector changes is who answers the call.
"""

from __future__ import annotations

from decimal import Decimal

from app.config import Settings
from app.domain.errors import ConfigError
from app.models.base import ModelCatalogue, ModelClient
from app.models.router import CostCollector, ModelRouter, RoutingTable, catalogue_for


def build_model_client(
    settings: Settings, routing: RoutingTable, collector: CostCollector
) -> ModelClient:
    primary = _client_for(settings, catalogue_for(routing, "primary"))
    fallback = _client_for(settings, catalogue_for(routing, "fallback"))

    return ModelRouter(
        routing=routing,
        primary=primary,
        fallback=fallback,
        collector=collector,
        usd_to_inr=Decimal(str(settings.usd_to_inr)),
    )


def _client_for(settings: Settings, catalogue: ModelCatalogue) -> ModelClient:
    backend = settings.models_backend
    if backend == "novita":
        from app.models.impl.novita import NovitaModelClient

        return NovitaModelClient(
            api_key=settings.novita_api_key,
            base_url=settings.novita_base_url,
            catalogue=catalogue,
            default_timeout_seconds=settings.model_timeout_seconds,
            max_attempts=settings.model_max_attempts,
        )
    if backend == "fake":
        from app.models.impl.fake import FakeModelClient

        return FakeModelClient(catalogue=catalogue)
    raise ConfigError(f"unknown SLIPWAY_MODELS_BACKEND: {backend!r}")
