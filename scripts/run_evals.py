#!/usr/bin/env python3
"""Run the eval cases in evals/cases against the configured models seam.

Each case names a stage, an input and a list of assertions about the output.
Assertions are properties -- "the specification names its open questions" --
not string equality, because a prompt change that improves the output must not
fail for changing the words.

    make eval

This spends real tokens when the models seam is Novita. The spend per case is
printed. With SLIPWAY_MODELS_BACKEND=fake it runs against the fake client,
which is useful for checking the harness and useless for checking a prompt.
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orchestrator"))

from app.agents.prompts import PromptLibrary  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.domain.errors import SlipwayError  # noqa: E402
from app.models.base import Completion, Message  # noqa: E402
from app.models.factory import build_model_client  # noqa: E402
from app.services.catalogue import load_catalogue  # noqa: E402

STAGE_ROLE = {"specify": "spec", "build": "build", "test": "test", "review": "review"}


@dataclass(frozen=True)
class Failure:
    case: str
    assertion: str
    detail: str


def check(assertion: dict[str, Any], output: str) -> str | None:
    """Return a failure message, or None if the assertion holds."""
    kind = assertion.get("kind")
    lowered = output.lower()

    if kind == "contains_section":
        wanted = str(assertion["value"]).lower()
        return None if wanted in lowered else f"no section named {assertion['value']!r}"

    if kind == "not_contains":
        for value in assertion["values"]:
            if str(value).lower() in lowered:
                return f"output contains {value!r}"
        return None

    if kind == "mentions_any":
        if any(str(value).lower() in lowered for value in assertion["values"]):
            return None
        return f"output mentions none of {assertion['values']}"

    if kind == "verdict_is":
        wanted = str(assertion["value"]).lower()
        match = re.search(r"verdict\s*[:\-]?\s*`?(\w+)", lowered)
        if match is None:
            return "no verdict found in the output"
        return None if match.group(1) == wanted else f"verdict was {match.group(1)!r}"

    if kind == "numbered_list_under":
        section = str(assertion["section"]).lower()
        index = lowered.find(section)
        if index < 0:
            return f"no section named {assertion['section']!r}"
        tail = output[index:]
        items = re.findall(r"^\s*\d+[.)]\s+\S", tail, flags=re.MULTILINE)
        minimum = int(assertion["minimum"])
        if len(items) >= minimum:
            return None
        return f"found {len(items)} numbered items under {assertion['section']!r}, wanted {minimum}"

    return f"unknown assertion kind {kind!r}"


async def run_case(client: Any, prompts: PromptLibrary, path: Path, timeout: float) -> list[Failure]:
    case = yaml.safe_load((path / "case.yaml").read_text())
    stage = str(case["stage"])
    role = STAGE_ROLE.get(stage)
    if role is None:
        return [Failure(path.name, "stage", f"unknown stage {stage!r}")]

    rendered = prompts.render(
        stage,
        brief=str(case.get("brief", "")),
        spec=str(case.get("spec", "")),
        build_log=str(case.get("build_log", "")),
    )
    result = await client.complete(
        role=role,
        messages=[
            Message(role="system", content=prompts.get("system")),
            Message(role="user", content=rendered),
        ],
        timeout_seconds=timeout,
    )

    if not isinstance(result, Completion):
        return [Failure(path.name, "completion", f"{result.reason}: {result.detail}")]

    print(f"  {path.name}: {result.usage.total_tokens} tokens via {result.model_id}")

    failures = []
    for assertion in case.get("assertions", []):
        problem = check(assertion, result.text)
        if problem is not None:
            failures.append(Failure(path.name, str(assertion.get("kind")), problem))
    return failures


async def main(directory: Path) -> int:
    settings = get_settings()
    catalogue = load_catalogue(
        settings.models_catalogue_path, require_models=settings.models_backend != "fake"
    )
    client = build_model_client(settings, catalogue)
    prompts = PromptLibrary(ROOT / "prompts")

    cases = sorted(p for p in directory.iterdir() if (p / "case.yaml").is_file())
    if not cases:
        print(f"no cases in {directory}", file=sys.stderr)
        return 1

    print(f"Running {len(cases)} cases against the {settings.models_backend} models backend")
    if settings.models_backend == "fake":
        print("  (fake backend: this checks the harness, not the prompts)")

    results = await asyncio.gather(
        *(run_case(client, prompts, case, settings.model_timeout_seconds) for case in cases)
    )
    failures = [failure for group in results for failure in group]

    print()
    if not failures:
        print(f"{len(cases)} cases passed")
        return 0

    for failure in failures:
        print(f"FAIL {failure.case} [{failure.assertion}]: {failure.detail}")
    print(f"\n{len(failures)} assertions failed across {len(cases)} cases")
    return 1


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "evals" / "cases"
    try:
        raise SystemExit(asyncio.run(main(target)))
    except SlipwayError as exc:
        raise SystemExit(f"eval: {exc}") from exc
