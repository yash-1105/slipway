#!/usr/bin/env python3
"""Insert local development fixtures.

Contains no secret, no token and no client name -- it is committed, and
everything in it is visible to anyone with the repository. Safe to re-run: it
does nothing if runs already exist.

    make seed
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orchestrator"))

from app.config import get_settings  # noqa: E402
from app.container import build_container  # noqa: E402
from app.domain.errors import SlipwayError  # noqa: E402

BRIEFS = [
    (
        "Sample: booking form",
        "A single-page booking form for a small clinic. A visitor picks a date "
        "and a time from the slots that are free, leaves a name and an email, "
        "and gets a confirmation on screen.",
    ),
    (
        "Sample: invoice extractor",
        "An internal page that takes an uploaded PDF invoice and shows the "
        "supplier, the total and the due date in a table, with the extracted "
        "values editable before they are saved.",
    ),
    (
        "Sample: underspecified on purpose",
        "We want a dashboard for the sales team.",
    ),
]


async def main() -> int:
    settings = get_settings()
    container = build_container(settings)
    try:
        existing = await container.runs.list_runs(limit=1)
        if existing:
            print("database already has runs; seed is a no-op")
            return 0

        for title, brief in BRIEFS:
            run = await container.runs.create(brief, title=title)
            print(f"{run.id}\t{run.state.value}\t{title}")

        print(f"\nseeded {len(BRIEFS)} runs")
        return 0
    finally:
        await container.aclose()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except SlipwayError as exc:
        raise SystemExit(f"seed: {exc}") from exc
