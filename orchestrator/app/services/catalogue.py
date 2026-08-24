"""Loading config/models.yaml.

The file is parsed by app/models/router.py, which owns its shape. This module
derives the flat role -> model view that the health endpoint and
`slipway models catalogue` display, so there is one parser and one place the
schema is defined.

The catalogue is generated from Novita's live /models endpoint by
scripts/sync_models.py. It is never hand-edited, and no model id is ever typed
from memory anywhere in this codebase -- call sites ask for a role.
"""

from __future__ import annotations

from pathlib import Path

from app.models.base import ModelCatalogue
from app.models.router import RoutingTable, catalogue_for, load_routing


def load_catalogue(path: Path, *, require_models: bool) -> ModelCatalogue:
    """The primary model for each role, for display."""
    return catalogue_for(load_routing(path, require_models=require_models), "primary")


def load_routing_table(path: Path, *, require_models: bool) -> RoutingTable:
    """The full routing, including fallbacks and prices."""
    return load_routing(path, require_models=require_models)
