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
    # Swapped to DeepSeek V4 Flash after the tool-calling bake-off: it beat
    # GLM 4.7 8/9 to 7/9, passed the refactor step twice where GLM 4.7 passed
    # neither, and cost a quarter as much. GLM 4.7's failure mode -- inventing
    # tests for behaviour the code never had -- is disqualifying for the role
    # that authors tests. See docs/decisions/0009-tool-calling-substrate.md.
    "test_author": "DeepSeek V4 Flash",
    "doc_writer": "DeepSeek V4 Flash",
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


def probe_responses_api(base_url: str, api_key: str, model_id: str) -> tuple[int, str]:
    """Does {base_url}/responses exist?

    Probed with a real model id. An earlier version sent an empty one and read
    the resulting `404 MODEL_NOT_FOUND` as "no such route" -- but that 404 was
    about the model, not the endpoint. A missing route answers `404 page not
    found`; a route that exists rejects a model it does not serve with a 400.
    The difference is the whole answer, and it is invisible without a real id.
    """
    try:
        response = httpx.post(
            f"{base_url}/responses",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model_id, "input": "ping"},
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return (0, f"transport error: {type(exc).__name__}")

    body = response.text[:200].replace("\n", " ").strip()
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


def assign(
    models: dict[str, dict[str, Any]], role: str, primary_id: str, fallback_id: str
) -> dict[str, Any]:
    """Build a role entry from the live model objects.

    Every number here -- both prices, the context window, the output cap -- is
    read off the provider's own response. A price typed by hand is a price that
    is wrong the first time the provider changes it, and a ledger built on it is
    wrong quietly.

    Both ids are verified against the live list, so an id that no longer exists
    fails here rather than at the first agent call.
    """
    entry: dict[str, Any] = {"intent": INTENDED.get(role, "")}

    for slot, model_id in (("primary", primary_id), ("fallback", fallback_id)):
        model = models.get(model_id)
        if model is None:
            raise SystemExit(
                f"{role}.{slot}: {model_id!r} is not in the provider's model list. "
                "Choose one from `available`."
            )

        pricing = model.get("pricing") or {}
        prompt_price = (pricing.get("prompt") or {}).get("price_per_m_decimal")
        completion_price = (pricing.get("completion") or {}).get("price_per_m_decimal")
        if prompt_price is None or completion_price is None:
            raise SystemExit(
                f"{role}.{slot}: {model_id!r} publishes no price. Slipway will not "
                "route to a model it cannot cost."
            )

        entry[slot] = {
            "model_id": model_id,
            "input_usd_per_mtok": str(prompt_price),
            "output_usd_per_mtok": str(completion_price),
            "display_name": str(model.get("display_name", "")),
            "supports_responses_endpoint": "responses" in (model.get("endpoints") or []),
        }

    primary = models[primary_id]
    # The pair has to share a ceiling, because a caller sized a prompt for the
    # role, not for whichever model answered. Take the smaller of the two.
    fallback = models[fallback_id]
    entry["context_window"] = min(
        int(primary.get("context_size", 0)), int(fallback.get("context_size", 0))
    )
    entry["max_output_tokens"] = min(
        int(primary.get("max_output_tokens", 0)), int(fallback.get("max_output_tokens", 0))
    )
    return entry


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

    # A model the provider actually serves, so a 404 means the route is missing
    # rather than the model being unknown.
    sample_model = ids[0]
    status, body = probe_responses_api(base_url, api_key, sample_model)
    # `404 page not found` is a missing route. A 400 is a route that exists and
    # rejected this model.
    supported = status != 0 and not (status == 404 and "page not found" in body.lower())
    print(f"Responses API at {base_url}/responses (probed with {sample_model}): "
          f"HTTP {status or 'error'}")
    print(f"  {body}")
    print(
        "  -> the route is absent; chat completions only."
        if not supported
        else "  -> THE ROUTE EXISTS. CLAUDE.md says Novita has no Responses API. "
             "Reporting, not acting on it."
    )
    print()

    existing = yaml.safe_load(CATALOGUE.read_text()) if CATALOGUE.is_file() else {}
    existing = existing if isinstance(existing, dict) else {}
    roles = existing.get("roles") or {}
    # Preserved like `roles`: a human decision the sync must not overwrite.
    # Models a bake-off should compare for a role, beyond the one assigned. A
    # newer model at the same price is a spec sheet, not evidence; this is where
    # it waits for the harness to produce some.
    bakeoff = existing.get("bakeoff_candidates") or {}

    for role, entry in list(roles.items()):
        for slot in ("primary", "fallback"):
            model_id = (entry.get(slot) or {}).get("model_id") if isinstance(entry, dict) else None
            if model_id and model_id not in ids:
                print(
                    f"WARNING: {role}.{slot} points at {model_id!r}, which the "
                    "provider no longer lists.",
                    file=sys.stderr,
                )

    for role, candidates in bakeoff.items():
        for model_id in candidates or []:
            if model_id not in ids:
                print(
                    f"WARNING: bakeoff candidate {role}.{model_id!r} is no longer "
                    "listed by the provider.",
                    file=sys.stderr,
                )

    print("Proposed matches for the intended families (confirm before use):")
    for role, intent in INTENDED.items():
        matches = propose(intent, ids)
        assigned = "assigned" if role in roles else "UNASSIGNED"
        print(f"  {role:<12} {intent:<20} [{assigned}]")
        for model_id in matches or ["  (no candidate matched -- choose from `available`)"]:
            print(f"      {model_id}")

    # --assign role=primary_id,fallback_id -- repeatable. The ids come from the
    # list printed above; the prices come from the provider.
    by_id = {str(m["id"]): m for m in models if m.get("id")}
    for argument in sys.argv[1:]:
        if not argument.startswith("--assign="):
            raise SystemExit(f"unknown argument {argument!r}; expected --assign=role=a,b")
        role, _, pair = argument.removeprefix("--assign=").partition("=")
        primary_id, _, fallback_id = pair.partition(",")
        if not (role and primary_id and fallback_id):
            raise SystemExit(f"malformed {argument!r}; expected --assign=role=primary,fallback")
        if role not in INTENDED:
            raise SystemExit(f"unknown role {role!r}; expected one of {', '.join(INTENDED)}")
        roles[role] = assign(by_id, role, primary_id, fallback_id)
        print(f"assigned {role}: {primary_id} -> {fallback_id}")

    document = {
        "source_base_url": base_url,
        "synced_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "responses_api_endpoint_exists": supported,
        "models_supporting_responses_endpoint": sorted(
            str(m["id"]) for m in models if "responses" in (m.get("endpoints") or [])
        ),
        "roles": roles,
        "bakeoff_candidates": bakeoff,
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
        "#\n"
        "# `bakeoff_candidates` is preserved across syncs too. It lists the models\n"
        "# a bake-off should compare for a role, beyond the one assigned. A newer\n"
        "# model at the same price is a spec sheet, not evidence; it goes here and\n"
        "# waits for the harness to produce some. Nothing reads it at runtime, so\n"
        "# two tests keep it honest: every candidate must still be listed by the\n"
        "# provider, and the assigned primary must be among its own candidates --\n"
        "# a comparison without the incumbent has no baseline to beat.\n"
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
