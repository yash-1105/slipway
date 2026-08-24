"""Loading config/models.yaml. Reads a real file, so not a unit test."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.domain.errors import ConfigError
from app.services.catalogue import load_catalogue

POPULATED = {
    "source_base_url": "https://api.novita.ai/openai/v1",
    "synced_at": "2026-08-24T00:00:00+00:00",
    "roles": {
        "spec": {
            "model_id": "some/model-id-from-the-provider",
            "context_window": 128000,
            "max_output_tokens": 8192,
            "notes": "chosen for long briefs",
        }
    },
    "available": ["some/model-id-from-the-provider"],
}


def write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(document))
    return path


def test_a_populated_catalogue_resolves_a_role(tmp_path: Path) -> None:
    catalogue = load_catalogue(write(tmp_path, POPULATED), require_models=True)

    spec = catalogue.resolve("spec")
    assert spec is not None
    assert spec.model_id == "some/model-id-from-the-provider"
    assert catalogue.synced_at == "2026-08-24T00:00:00+00:00"


def test_an_unconfigured_role_resolves_to_nothing_rather_than_a_guess(tmp_path: Path) -> None:
    """No model id is ever invented; an unassigned role must come back empty."""
    catalogue = load_catalogue(write(tmp_path, POPULATED), require_models=True)
    assert catalogue.resolve("build") is None


def test_the_unpopulated_catalogue_that_ships_in_the_repo_is_refused() -> None:
    """It ships empty on purpose, and must stop the process rather than be used."""
    shipped = Path(__file__).resolve().parents[3] / "config" / "models.yaml"
    with pytest.raises(ConfigError, match="models-sync"):
        load_catalogue(shipped, require_models=True)


def test_an_empty_catalogue_is_allowed_when_the_models_seam_is_fake(tmp_path: Path) -> None:
    """A checkout with no Novita key still boots for tests and frontend work."""
    catalogue = load_catalogue(tmp_path / "absent.yaml", require_models=False)
    assert catalogue.by_role == {}
    assert catalogue.synced_at == "never"


def test_a_missing_catalogue_is_refused_when_models_are_required(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="make models-sync"):
        load_catalogue(tmp_path / "absent.yaml", require_models=True)


def test_a_role_missing_a_field_names_the_field(tmp_path: Path) -> None:
    broken = {**POPULATED, "roles": {"spec": {"model_id": "x", "context_window": 1}}}
    with pytest.raises(ConfigError, match="max_output_tokens"):
        load_catalogue(write(tmp_path, broken), require_models=True)


def test_a_catalogue_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not a YAML mapping"):
        load_catalogue(write(tmp_path, ["not", "a", "mapping"]), require_models=True)
