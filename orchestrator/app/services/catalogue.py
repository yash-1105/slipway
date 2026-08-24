"""Loading config/models.yaml.

The catalogue is generated from Novita's live /models endpoint by
scripts/sync_models.py. It is never hand-edited, and no model id is ever typed
from memory anywhere in this codebase -- call sites ask for a role.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.domain.errors import ConfigError
from app.models.base import ModelCatalogue, ModelSpec


def load_catalogue(path: Path, *, require_models: bool) -> ModelCatalogue:
    """Read the catalogue, or fail loudly at startup.

    `require_models` is False when the models seam is the fake one, so a
    checkout with no Novita key still boots for tests and frontend work.
    """
    if not path.is_file():
        if not require_models:
            return ModelCatalogue(source_base_url="", synced_at="never", by_role={})
        raise ConfigError(
            f"{path} does not exist. Run `make models-sync` with a live "
            "SLIPWAY_NOVITA_API_KEY to generate it from the provider's /models "
            "endpoint. Do not write it by hand."
        )

    raw: Any = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} is not a YAML mapping")

    roles_raw = raw.get("roles") or {}
    if not isinstance(roles_raw, dict):
        raise ConfigError(f"{path}: `roles` must be a mapping of role -> model")

    by_role: dict[str, ModelSpec] = {}
    for role, entry in roles_raw.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: role {role!r} must be a mapping")
        try:
            by_role[str(role)] = ModelSpec(
                role=str(role),
                model_id=str(entry["model_id"]),
                context_window=int(entry["context_window"]),
                max_output_tokens=int(entry["max_output_tokens"]),
                notes=str(entry.get("notes", "")),
            )
        except KeyError as exc:
            raise ConfigError(f"{path}: role {role!r} is missing {exc.args[0]!r}") from exc

    if require_models and not by_role:
        raise ConfigError(
            f"{path} has no roles. It has not been populated from the live "
            "/models endpoint yet -- run `make models-sync`. See "
            "docs/decisions/0004-novita-base-url.md."
        )

    return ModelCatalogue(
        source_base_url=str(raw.get("source_base_url", "")),
        synced_at=str(raw.get("synced_at", "unknown")),
        by_role=by_role,
    )
