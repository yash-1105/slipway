"""models seam factory.

Reads one selector, SLIPWAY_MODELS_BACKEND, through app/config.py. Concrete
implementations are imported inside the function so that selecting one does not
import the others, and so nothing above the seam can reach them.
"""

from __future__ import annotations

from app.config import Settings
from app.domain.errors import ConfigError
from app.models.base import ModelCatalogue, ModelClient


def build_model_client(settings: Settings, catalogue: ModelCatalogue) -> ModelClient:
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
