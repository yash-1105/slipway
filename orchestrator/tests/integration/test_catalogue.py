"""Parsing config/models.yaml. Reads a real file, so not a unit test."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.domain.errors import ConfigError
from app.models.router import load_routing
from app.services.catalogue import load_catalogue


def role(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "intent": "an intended model family",
        "context_window": 128000,
        "max_output_tokens": 8192,
        "primary": {
            "model_id": "vendor-a/primary-model",
            "input_usd_per_mtok": 2,
            "output_usd_per_mtok": 10,
        },
        "fallback": {
            "model_id": "vendor-b/fallback-model",
            "input_usd_per_mtok": 1,
            "output_usd_per_mtok": 4,
        },
    }
    entry.update(overrides)
    return entry


ALL_ROLES = ["planner", "builder", "evaluator", "test_author", "doc_writer"]

POPULATED: dict[str, Any] = {
    "source_base_url": "https://api.novita.ai/openai/v1",
    "synced_at": "2026-08-24T00:00:00+00:00",
    "roles": {name: role() for name in ALL_ROLES},
    "available": ["vendor-a/primary-model", "vendor-b/fallback-model"],
}


def write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(document))
    return path


def test_a_populated_file_resolves_every_role(tmp_path: Path) -> None:
    routing = load_routing(write(tmp_path, POPULATED), require_models=True)

    for name in ALL_ROLES:
        entry = routing.get_model(name)
        assert entry.primary.model_id == "vendor-a/primary-model"
        assert entry.fallback.model_id == "vendor-b/fallback-model"
        assert entry.primary.input_usd_per_mtok == Decimal("2")


def test_prices_are_decimals_not_floats(tmp_path: Path) -> None:
    routing = load_routing(write(tmp_path, POPULATED), require_models=True)
    pricing = routing.get_model("planner").primary

    assert isinstance(pricing.input_usd_per_mtok, Decimal)
    assert isinstance(pricing.output_usd_per_mtok, Decimal)


def test_the_flat_catalogue_shows_the_primary(tmp_path: Path) -> None:
    catalogue = load_catalogue(write(tmp_path, POPULATED), require_models=True)

    spec = catalogue.resolve("planner")
    assert spec is not None
    assert spec.model_id == "vendor-a/primary-model"


def test_a_role_that_is_not_configured_names_itself(tmp_path: Path) -> None:
    """No model id is ever invented; an unassigned role must stop the caller."""
    partial = {**POPULATED, "roles": {"planner": role()}}
    routing = load_routing(write(tmp_path, partial), require_models=False)

    with pytest.raises(ConfigError, match="builder"):
        routing.get_model("builder")


def test_a_file_missing_a_role_is_refused_at_startup(tmp_path: Path) -> None:
    partial = {**POPULATED, "roles": {"planner": role()}}

    with pytest.raises(ConfigError, match="builder, evaluator, test_author, doc_writer"):
        load_routing(write(tmp_path, partial), require_models=True)


def test_the_file_that_ships_in_the_repo_configures_every_role() -> None:
    """The committed catalogue is the one a fresh checkout starts the seam with.

    It was empty until the live probe ran; a checkout that cannot start the
    models seam is worse than one whose ids are stale, and stale ids are caught
    by re-running the sync.
    """
    shipped = Path(__file__).resolve().parents[3] / "config" / "models.yaml"
    routing = load_routing(shipped, require_models=True)

    assert routing.source_base_url == "https://api.novita.ai/openai/v1"
    for name in ALL_ROLES:
        entry = routing.get_model(name)
        assert entry.primary.model_id in routing.available
        assert entry.fallback.model_id in routing.available
        assert entry.primary.model_id != entry.fallback.model_id
        assert entry.primary.input_usd_per_mtok > 0
        assert entry.primary.output_usd_per_mtok > 0
        assert entry.context_window > 0


def test_no_role_falls_back_to_its_own_primary() -> None:
    """A fallback that is the primary is not a fallback."""
    shipped = Path(__file__).resolve().parents[3] / "config" / "models.yaml"
    routing = load_routing(shipped, require_models=True)

    for name in ALL_ROLES:
        entry = routing.get_model(name)
        assert entry.primary.model_id != entry.fallback.model_id


def test_an_empty_file_is_allowed_when_the_models_seam_is_fake(tmp_path: Path) -> None:
    routing = load_routing(tmp_path / "absent.yaml", require_models=False)

    assert routing.by_role == {}
    assert routing.synced_at == "never"


def test_a_missing_file_is_refused_when_models_are_required(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="make models-sync"):
        load_routing(tmp_path / "absent.yaml", require_models=True)


def test_a_role_without_a_fallback_is_refused(tmp_path: Path) -> None:
    """Every role has a fallback. A role with one model has no answer to an outage."""
    without_fallback = {k: v for k, v in role().items() if k != "fallback"}
    broken = {**POPULATED, "roles": {"planner": without_fallback}}

    with pytest.raises(ConfigError, match="fallback"):
        load_routing(write(tmp_path, broken), require_models=False)


def test_a_model_without_a_price_is_refused(tmp_path: Path) -> None:
    """An unpriced model produces ledger rows that cannot be totalled."""
    priceless = role(primary={"model_id": "vendor-a/primary-model"})
    broken = {**POPULATED, "roles": {"planner": priceless}}

    with pytest.raises(ConfigError, match="input_usd_per_mtok"):
        load_routing(write(tmp_path, broken), require_models=False)


def test_a_negative_price_is_refused(tmp_path: Path) -> None:
    negative = role(
        primary={
            "model_id": "vendor-a/primary-model",
            "input_usd_per_mtok": -1,
            "output_usd_per_mtok": 10,
        }
    )
    broken = {**POPULATED, "roles": {"planner": negative}}

    with pytest.raises(ConfigError, match="negative"):
        load_routing(write(tmp_path, broken), require_models=False)


def test_an_empty_model_id_is_refused(tmp_path: Path) -> None:
    """A blank id is what an unfinished sync leaves behind."""
    blank = role(
        primary={"model_id": "  ", "input_usd_per_mtok": 1, "output_usd_per_mtok": 2}
    )
    broken = {**POPULATED, "roles": {"planner": blank}}

    with pytest.raises(ConfigError, match="model_id is empty"):
        load_routing(write(tmp_path, broken), require_models=False)


def test_a_file_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not a YAML mapping"):
        load_routing(write(tmp_path, ["not", "a", "mapping"]), require_models=True)


def test_the_available_list_is_carried_through(tmp_path: Path) -> None:
    routing = load_routing(write(tmp_path, POPULATED), require_models=True)

    assert routing.available == ("vendor-a/primary-model", "vendor-b/fallback-model")
    assert routing.source_base_url == "https://api.novita.ai/openai/v1"
