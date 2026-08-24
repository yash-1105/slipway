#!/usr/bin/env python3
"""Regenerate config/models.yaml from Novita's live /models endpoint.

Also settles docs/decisions/0004-novita-base-url.md: Novita's documentation
shows three candidate base paths and only one of them is right. This probes all
three and reports what each returned, so the ADR records an observation rather
than a guess.

Usage:
    export SLIPWAY_NOVITA_API_KEY=...
    make models-sync
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = ROOT / "config" / "models.yaml"

#: The three paths Novita's docs show. Probed in order; the first that returns
#: a usable model list wins, and all three results are printed for the ADR.
CANDIDATES = (
    "https://api.novita.ai/openai/v1",
    "https://api.novita.ai/openai",
    "https://api.novita.ai/v3/openai",
)

TIMEOUT_SECONDS = 30.0

#: Which logical role gets which model is a human decision. The sync preserves
#: existing choices and never invents one -- it only refreshes the available
#: list and flags a role whose model the provider no longer offers.
ROLES = ("spec", "build", "test", "review")


def probe(base_url: str, api_key: str) -> tuple[int, list[dict[str, Any]], str]:
    """GET {base_url}/models. Returns (status, models, note)."""
    try:
        response = httpx.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return (0, [], f"transport error: {exc}")

    if response.status_code != 200:
        return (response.status_code, [], response.text[:200].replace("\n", " "))

    try:
        payload = response.json()
    except ValueError:
        return (response.status_code, [], "200 but the body was not JSON")

    models = payload.get("data")
    if not isinstance(models, list):
        return (response.status_code, [], "200 but no `data` list in the body")

    return (response.status_code, models, "ok")


def main() -> int:
    api_key = os.environ.get("SLIPWAY_NOVITA_API_KEY", "")
    if not api_key:
        print(
            "SLIPWAY_NOVITA_API_KEY is not set. This script makes a real call; "
            "it cannot be run without a key.",
            file=sys.stderr,
        )
        return 2

    results: list[tuple[str, int, int, str]] = []
    winner: tuple[str, list[dict[str, Any]]] | None = None

    for base_url in CANDIDATES:
        status, models, note = probe(base_url, api_key)
        results.append((base_url, status, len(models), note))
        if models and winner is None:
            winner = (base_url, models)

    print("Candidate base URLs (paste this table into docs/decisions/0004-novita-base-url.md):")
    print()
    print(f"| {'Candidate base URL':<38} | {'Status':<6} | {'Models':<6} | Note")
    print(f"| {'-' * 38} | {'-' * 6} | {'-' * 6} | ----")
    for base_url, status, count, note in results:
        shown = str(status) if status else "error"
        print(f"| `{base_url}`{' ' * max(0, 36 - len(base_url))} | {shown:<6} | {count:<6} | {note}")
    print()

    if winner is None:
        print("No candidate returned a model list. Nothing written.", file=sys.stderr)
        return 1

    base_url, models = winner
    ids = sorted(str(model.get("id", "")) for model in models if model.get("id"))

    existing = yaml.safe_load(CATALOGUE.read_text()) if CATALOGUE.is_file() else {}
    existing = existing if isinstance(existing, dict) else {}
    roles = existing.get("roles") or {}

    for role, entry in list(roles.items()):
        model_id = entry.get("model_id") if isinstance(entry, dict) else None
        if model_id and model_id not in ids:
            print(
                f"WARNING: role {role!r} points at {model_id!r}, which the provider "
                "no longer lists. Pick a replacement from `available` below.",
                file=sys.stderr,
            )

    missing = [role for role in ROLES if role not in roles]

    document = {
        "source_base_url": base_url,
        "synced_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "roles": roles,
        "available": ids,
    }

    header = (
        "# GENERATED FILE -- do not edit by hand.\n"
        "#\n"
        "# Written by scripts/sync_models.py from a live GET {base_url}/models.\n"
        "# Every model id in Slipway is read from here; none is typed from memory.\n"
        "# See docs/decisions/0004-novita-base-url.md.\n"
        "#\n"
        "# `roles` is a human decision and is preserved across syncs. To assign one:\n"
        "#   roles:\n"
        "#     spec:\n"
        "#       model_id: <copy an id from `available` below>\n"
        "#       context_window: 128000\n"
        "#       max_output_tokens: 8192\n"
        "#       notes: why this model for this role\n"
        "\n"
    )
    CATALOGUE.write_text(header + yaml.safe_dump(document, sort_keys=False, width=100))

    print(f"Wrote {CATALOGUE.relative_to(ROOT)}: {len(ids)} models from {base_url}")
    if missing:
        print(
            f"\nRoles still unassigned: {', '.join(missing)}. "
            "Slipway will refuse to start until each one names a model from `available`."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
