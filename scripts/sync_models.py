#!/usr/bin/env python3
"""Probe Novita's base URL candidates and regenerate config/models.yaml.

Settles docs/decisions/0004-novita-base-url.md: Novita's documentation shows
three candidate base paths and only one can be right. This probes all three,
prints what each returned, and writes the winner's model list.

It also checks whether the Responses API exists at the working base URL. Slipway
uses chat completions on the strength of a documented claim; a claim is not an
observation, and this makes it one.

Model ids are written EXACTLY as the provider returns them. Role assignment is a
human decision: the script proposes matches for the intended families and never
assigns one on its own.

    export SLIPWAY_NOVITA_API_KEY=...
    make models-sync
"""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = ROOT / "config" / "models.yaml"

#: The three paths Novita's docs show. All are probed; the first that returns a
#: usable model list wins, and every result is printed for the ADR.
CANDIDATES = (
    "https://api.novita.ai/openai/v1",
    "https://api.novita.ai/openai",
    "https://api.novita.ai/v3/openai",
)

TIMEOUT_SECONDS = 30.0

#: The roles app/models/router.py routes, and the model family a human intended
#: for each. These are families in a person's words, not model ids: the id comes
#: from the provider and is never typed here. The script matches these against
#: what /models returns and proposes; a human confirms.
INTENDED: dict[str, str] = {
    "planner": "GLM 5.2",
    "builder": "Kimi K2.7 Code",
    "evaluator": "DeepSeek V4 Flash",
    "test_author": "GLM 4.7",
    "doc_writer": "GLM 4.7",
}


def probe(base_url: str, api_key: str) -> tuple[int, list[dict[str, Any]], str]:
    """GET {base_url}/models. Returns (status, models, note)."""
    try:
        response = httpx.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return (0, [], f"transport error: {type(exc).__name__}")

    if response.status_code != 200:
        return (response.status_code, [], response.text[:120].replace("\n", " ").strip())

    try:
        payload = response.json()
    except ValueError:
        return (response.status_code, [], "200 but the body was not JSON")

    models = payload.get("data")
    if not isinstance(models, list):
        return (response.status_code, [], "200 but no `data` list in the body")

    return (response.status_code, models, "ok")


def probe_responses_api(base_url: str, api_key: str) -> tuple[int, str]:
    """Does {base_url}/responses exist?

    CLAUDE.md states Novita has no Responses API and the client must use chat
    completions. That is a documented claim; this turns it into an observation.
    A 404 or 405 confirms it. Anything that looks like the endpoint exists is
    reported, not acted on.
    """
    try:
        response = httpx.post(
            f"{base_url}/responses",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "", "input": "ping"},
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return (0, f"transport error: {type(exc).__name__}")

    body = response.text[:160].replace("\n", " ").strip()
    return (response.status_code, body)


def _normalise(text: str) -> str:
    """Lowercase alphanumerics, for comparing a family name to a model id."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def propose(intent: str, model_ids: list[str]) -> list[str]:
    """Model ids that plausibly match an intended family, best first.

    Deliberately dumb: it narrows a long list for a human, and does not decide.
    """
    wanted = _normalise(intent)
    scored: list[tuple[int, str]] = []
    for model_id in model_ids:
        haystack = _normalise(model_id)
        if wanted and wanted in haystack:
            scored.append((0, model_id))
            continue
        tokens = [_normalise(t) for t in intent.split() if _normalise(t)]
        hits = sum(1 for token in tokens if token in haystack)
        if hits >= max(1, len(tokens) - 1):
            scored.append((len(tokens) - hits, model_id))
    return [model_id for _, model_id in sorted(scored)][:5]


def main() -> int:
    api_key = os.environ.get("SLIPWAY_NOVITA_API_KEY", "")
    if not api_key:
        print(
            "SLIPWAY_NOVITA_API_KEY is not set. This script makes real calls; it "
            "cannot run without a key.",
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

    width = max(len(url) for url in CANDIDATES) + 2
    print("Base URL probe -- paste into docs/decisions/0004-novita-base-url.md:")
    print()
    print(f"| {'Candidate base URL':<{width}} | {'Status':<6} | {'Models':<6} | Note")
    print(f"| {'-' * width} | {'-' * 6} | {'-' * 6} | ----")
    for base_url, status, count, note in results:
        shown = str(status) if status else "error"
        print(f"| `{base_url}`{' ' * (width - len(base_url) - 2)} | {shown:<6} | {count:<6} | {note}")
    print()

    if winner is None:
        print("No candidate returned a model list. Nothing written.", file=sys.stderr)
        return 1

    base_url, models = winner
    ids = sorted({str(m.get("id", "")) for m in models if m.get("id")})

    status, body = probe_responses_api(base_url, api_key)
    supported = status not in (0, 404, 405, 501)
    print(f"Responses API at {base_url}/responses: HTTP {status or 'error'}")
    print(f"  {body}")
    print(
        "  -> absent, as documented; chat completions only."
        if not supported
        else "  -> DID NOT 404. This contradicts CLAUDE.md. Reporting, not acting on it."
    )
    print()

    existing = yaml.safe_load(CATALOGUE.read_text()) if CATALOGUE.is_file() else {}
    existing = existing if isinstance(existing, dict) else {}
    roles = existing.get("roles") or {}

    for role, entry in list(roles.items()):
        for slot in ("primary", "fallback"):
            model_id = (entry.get(slot) or {}).get("model_id") if isinstance(entry, dict) else None
            if model_id and model_id not in ids:
                print(
                    f"WARNING: {role}.{slot} points at {model_id!r}, which the "
                    "provider no longer lists.",
                    file=sys.stderr,
                )

    print("Proposed matches for the intended families (confirm before use):")
    for role, intent in INTENDED.items():
        matches = propose(intent, ids)
        assigned = "assigned" if role in roles else "UNASSIGNED"
        print(f"  {role:<12} {intent:<20} [{assigned}]")
        for model_id in matches or ["  (no candidate matched -- choose from `available`)"]:
            print(f"      {model_id}")

    document = {
        "source_base_url": base_url,
        "synced_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "responses_api_supported": supported,
        "roles": roles,
        "available": ids,
    }

    header = (
        "# GENERATED FILE -- do not edit by hand, except `roles`.\n"
        "#\n"
        "# Written by scripts/sync_models.py from a live GET {base_url}/models.\n"
        "# Model ids are exactly what the provider returned. None is typed from\n"
        "# memory. See docs/decisions/0004-novita-base-url.md.\n"
        "#\n"
        "# `roles` is a human decision and is preserved across syncs. Each role\n"
        "# needs a primary, a fallback, and both published prices:\n"
        "#\n"
        "#   roles:\n"
        "#     planner:\n"
        "#       intent: GLM 5.2            # the family a human asked for\n"
        "#       context_window: 128000\n"
        "#       max_output_tokens: 8192\n"
        "#       primary:\n"
        "#         model_id: <copy an id from `available` below>\n"
        "#         input_usd_per_mtok: <published price>\n"
        "#         output_usd_per_mtok: <published price>\n"
        "#       fallback:\n"
        "#         model_id: <a different id from `available`>\n"
        "#         input_usd_per_mtok: <published price>\n"
        "#         output_usd_per_mtok: <published price>\n"
        "\n"
    )
    CATALOGUE.write_text(header + yaml.safe_dump(document, sort_keys=False, width=100))

    print()
    print(f"Wrote {CATALOGUE.relative_to(ROOT)}: {len(ids)} models from {base_url}")
    unassigned = [r for r in INTENDED if r not in roles]
    if unassigned:
        print(
            f"Roles still unassigned: {', '.join(unassigned)}. The models seam "
            "refuses to start until each names a primary, a fallback and both prices."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
