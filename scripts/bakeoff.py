#!/usr/bin/env python3
"""Tool-calling bake-off: the go/no-go for the agent layer.

Runs the same four tasks against each candidate model in a throwaway git repo,
through a hand-written tool loop. Hand-written on purpose -- a framework that
repairs a malformed tool call is a framework that hides which model produces
them, and the malformed rate is the headline number here.

    python scripts/bakeoff.py --preflight        # confirm auth, spend nothing
    python scripts/bakeoff.py --budget-usd 15

Results are written to scripts/bakeoff_kit/results.json after every task, so a run
that stops on budget still reports everything it reached.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import openai
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from bakeoff_kit.tasks import TASKS, Task  # noqa: E402
from bakeoff_kit.tools import TOOLS, ToolStats, Workspace, validate  # noqa: E402

RESULTS = ROOT / "scripts" / "bakeoff_kit" / "results.json"
WORKDIR = Path("/private/tmp/claude-501/bakeoff")

#: Ceiling per task. A model that has not finished by here is not going to.
MAX_TURNS = {"T1": 30, "T2": 25, "T3": 30, "T4": 90}
MAX_OUTPUT_TOKENS = 3000
CALL_TIMEOUT_SECONDS = 180.0

SYSTEM = """You are a software engineer working in a small Python repository.

You have six tools: read, write, edit, bash, glob, grep. Use them to inspect the
repository before changing it, and to run the build and the tests.

Rules:
- Follow the conventions already present in the repository.
- Change only what the task asks for.
- Run the tests to confirm your work. `make build` runs the import check and the suite.
- When the task is complete, reply with a short summary and no tool call.
"""


def keychain_api_key() -> str:
    """Read the key from the keychain, per invocation. Never from the environment."""
    done = subprocess.run(
        ["security", "find-generic-password", "-a", subprocess.run(
            ["whoami"], capture_output=True, text=True).stdout.strip(),
         "-s", "slipway-novita-key", "-w"],
        capture_output=True, text=True,
    )
    if done.returncode != 0 or not done.stdout.strip():
        raise SystemExit("could not read slipway-novita-key from the keychain")
    return done.stdout.strip()


def candidates_and_prices(api_key: str) -> tuple[list[str], dict[str, tuple[Decimal, Decimal]]]:
    """Every primary and fallback in models.yaml, plus the bake-off candidates.

    Prices come from the live /models endpoint, not from models.yaml, because a
    challenger that is only a candidate has no entry there. models.yaml is read,
    never written.
    """
    catalogue = yaml.safe_load((ROOT / "config" / "models.yaml").read_text())

    ordered: list[str] = []

    def add(model_id: str) -> None:
        if model_id not in ordered:
            ordered.append(model_id)

    # Primaries first, then the challenger, then fallbacks: if the budget runs
    # out, the decisions that matter most are already covered.
    for entry in catalogue["roles"].values():
        add(entry["primary"]["model_id"])
    for candidates in (catalogue.get("bakeoff_candidates") or {}).values():
        for model_id in candidates:
            add(model_id)
    for entry in catalogue["roles"].values():
        add(entry["fallback"]["model_id"])

    response = httpx.get(
        f"{catalogue['source_base_url']}/models",
        headers={"Authorization": f"Bearer {api_key}"}, timeout=30,
    )
    response.raise_for_status()
    live = {m["id"]: m for m in response.json()["data"]}

    prices: dict[str, tuple[Decimal, Decimal]] = {}
    for model_id in ordered:
        model = live.get(model_id)
        if model is None:
            raise SystemExit(f"{model_id} is no longer listed by the provider")
        pricing = model["pricing"]
        prices[model_id] = (
            Decimal(str(pricing["prompt"]["price_per_m_decimal"])),
            Decimal(str(pricing["completion"]["price_per_m_decimal"])),
        )
    return ordered, prices


@dataclass
class TaskResult:
    task: str
    steps_total: int
    steps_passed: int
    step_results: list[bool]
    turns: int
    empty_responses: int
    seconds: float
    prompt_tokens: int
    completion_tokens: int
    usd: float
    total_calls: int
    malformed: int
    malformed_detail: list[str]
    missing_reference: int
    missing_reference_detail: list[str]
    outside_repo_attempts: int
    refused_bash: int
    out_of_scope_edits: list[str]
    new_files_outside_scope: list[str]
    stopped: str
    #: (call index, malformed, missing_reference) for degradation analysis.
    timeline: list[tuple[int, bool, bool]] = field(default_factory=list)


def materialise(task: Task, root: Path) -> None:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    for relative, content in task.files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for command in (
        "git init -q", "git add -A",
        'git -c user.email=b@k.e -c user.name=bakeoff commit -q -m fixture',
    ):
        subprocess.run(command, shell=True, cwd=root, capture_output=True)


def out_of_scope(root: Path, task: Task) -> tuple[list[str], list[str]]:
    """(existing files edited outside scope, new files added outside scope).

    Two different things, and only the first is a problem. A model that adds
    tests/test_helpers.py after writing helpers.py has done something good;
    counting that as scribbling outside its task would punish the behaviour we
    want. Editing a file the task never mentioned is the one worth reporting.
    """
    done = subprocess.run(
        "git status --porcelain", shell=True, cwd=root, capture_output=True, text=True
    )
    allowed = set(task.in_scope)
    edited: list[str] = []
    added: list[str] = []
    for line in done.stdout.splitlines():
        if not line.strip():
            continue
        code, name = line[:2], line[3:].strip()
        if name in allowed:
            continue
        (added if code.strip() == "??" else edited).append(name)
    return sorted(edited), sorted(added)


def run_task(
    client: openai.OpenAI, model_id: str, task: Task, prices: tuple[Decimal, Decimal],
    budget_left_usd: Decimal,
) -> TaskResult:
    root = WORKDIR / model_id.replace("/", "_") / task.id
    materialise(task, root)

    stats = ToolStats()
    workspace = Workspace(root, stats)
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM}]

    turns = 0
    empty_responses = 0
    consecutive_empty = 0
    prompt_tokens = completion_tokens = 0
    usd = Decimal(0)
    started = time.monotonic()
    stopped = "completed"
    step_results: list[bool] = []

    for step in task.steps:
        messages.append({"role": "user", "content": step.prompt})

        while True:
            if turns >= MAX_TURNS[task.id]:
                stopped = "turn cap"
                break
            if usd >= budget_left_usd:
                stopped = "budget"
                break

            try:
                response = client.chat.completions.create(
                    model=model_id, messages=messages, tools=TOOLS,
                    tool_choice="auto", temperature=0,
                    max_tokens=MAX_OUTPUT_TOKENS, timeout=CALL_TIMEOUT_SECONDS,
                )
            except openai.APIError as exc:
                stopped = f"api error: {type(exc).__name__}: {str(exc)[:160]}"
                break

            turns += 1
            if response.usage:
                prompt_tokens += response.usage.prompt_tokens
                completion_tokens += response.usage.completion_tokens
                usd += (Decimal(response.usage.prompt_tokens) / 1_000_000 * prices[0]
                        + Decimal(response.usage.completion_tokens) / 1_000_000 * prices[1])

            choice = response.choices[0].message
            calls = choice.tool_calls or []
            messages.append({
                "role": "assistant",
                "content": choice.content or "",
                **({"tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.function.name, "arguments": c.function.arguments}}
                    for c in calls]} if calls else {}),
            })

            if not calls:
                if (choice.content or "").strip():
                    break  # said its piece and stopped: the task is finished

                # An empty message with no tool call is not "done", it is a
                # stall. Treating the two as the same thing scored a model as
                # having decided the work was unnecessary when it had in fact
                # returned nothing at all. Nudge once, as any real loop would,
                # and record it either way.
                empty_responses += 1
                if consecutive_empty >= 1:
                    stopped = "stalled: two empty responses in a row"
                    break
                consecutive_empty += 1
                messages.append({
                    "role": "user",
                    "content": (
                        "You returned an empty response with no tool call. If the task "
                        "is complete, say so. Otherwise continue working on it."
                    ),
                })
                continue

            consecutive_empty = 0

            for call in calls:
                stats.total_calls += 1
                arguments, error = validate(call.function.name, call.function.arguments)

                if error is not None:
                    stats.malformed += 1
                    stats.malformed_detail.append(f"{call.function.name}: {error}")
                    stats.timeline.append((stats.total_calls, True, False))
                    result = f"error: {error}"
                else:
                    result, missing = workspace.run(call.function.name, arguments)
                    if missing:
                        stats.missing_reference += 1
                        stats.missing_reference_detail.append(
                            f"{call.function.name}: {json.dumps(arguments)[:120]}"
                        )
                    stats.timeline.append((stats.total_calls, False, missing))

                messages.append({
                    "role": "tool", "tool_call_id": call.id,
                    "content": result[:8000],
                })

        step_results.append(bool(step.check(root)))
        if stopped != "completed":
            step_results.extend([False] * (len(task.steps) - len(step_results)))
            break

    oos_edits, oos_added = out_of_scope(root, task)

    transcript = root.parent / f"{task.id}-transcript.json"
    transcript.write_text(json.dumps(messages, indent=2, default=str))

    return TaskResult(
        task=task.id,
        steps_total=len(task.steps),
        steps_passed=sum(step_results),
        step_results=step_results,
        turns=turns,
        empty_responses=empty_responses,
        seconds=round(time.monotonic() - started, 1),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        usd=float(round(usd, 6)),
        total_calls=stats.total_calls,
        malformed=stats.malformed,
        malformed_detail=stats.malformed_detail[:12],
        missing_reference=stats.missing_reference,
        missing_reference_detail=stats.missing_reference_detail[:12],
        outside_repo_attempts=stats.outside_repo_attempts,
        refused_bash=stats.refused_bash,
        out_of_scope_edits=oos_edits,
        new_files_outside_scope=oos_added,
        stopped=stopped,
        timeline=stats.timeline,
    )


def preflight(client: openai.OpenAI, models: list[str], prices: dict) -> None:
    print("preflight: authenticating and confirming tool calling per candidate\n")
    for model_id in models:
        try:
            response = client.chat.completions.create(
                model=model_id,
                messages=[{"role": "user", "content": "Call the glob tool with pattern '*.md'."}],
                tools=TOOLS, tool_choice="auto", max_tokens=200, timeout=90.0,
            )
            calls = response.choices[0].message.tool_calls or []
            usage = response.usage
            print(f"  {model_id:<32} ok   tool_calls={len(calls)}  "
                  f"tokens={usage.total_tokens if usage else '?'}")
        except openai.APIError as exc:
            print(f"  {model_id:<32} FAIL {type(exc).__name__}: {str(exc)[:100]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget-usd", type=float, default=15.0)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--only", default="", help="comma-separated model ids")
    parser.add_argument("--tasks", default="", help="comma-separated task ids")
    parser.add_argument("--out", default="", help="write results to a different file")
    parser.add_argument("--resume", action="store_true",
                        help="keep results already in results.json and run only what is missing")
    args = parser.parse_args()

    global RESULTS
    if args.out:
        RESULTS = ROOT / 'scripts' / 'bakeoff_kit' / args.out

    api_key = keychain_api_key()
    models, prices = candidates_and_prices(api_key)
    if args.only:
        wanted = {m.strip() for m in args.only.split(",")}
        models = [m for m in models if m in wanted]

    catalogue = yaml.safe_load((ROOT / "config" / "models.yaml").read_text())
    client = openai.OpenAI(
        api_key=api_key, base_url=catalogue["source_base_url"], max_retries=1,
        timeout=CALL_TIMEOUT_SECONDS,
    )

    if args.preflight:
        preflight(client, models, prices)
        return 0

    budget = Decimal(str(args.budget_usd))
    spent = Decimal(0)

    # Resume rather than redo. A run that was interrupted has already paid for
    # what it finished, and re-running it would spend that money again for the
    # same answer -- and, because these models are not deterministic even at
    # temperature 0, a slightly different one.
    previous: dict[str, Any] = {}
    if args.resume and RESULTS.is_file():
        previous = json.loads(RESULTS.read_text())
        spent = Decimal(str(previous.get("spent_usd", 0)))
        print(f"resuming: ${spent} already spent, "
              f"{sum(len(v) for v in previous.get('runs', {}).values())} task runs kept")

    results: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "base_url": catalogue["source_base_url"],
        "budget_usd": float(budget),
        "usd_to_inr": 95.5,
        "candidates": models,
        "prices": {m: [str(p[0]), str(p[1])] for m, p in prices.items()},
        "runs": previous.get("runs", {}),
        "not_reached": [],
    }

    def save() -> None:
        results["spent_usd"] = float(round(spent, 6))
        results["not_reached"] = [m for m in models if m not in results["runs"]]
        RESULTS.write_text(json.dumps(results, indent=2))

    tasks = [t for t in TASKS if not args.tasks or t.id in {
        x.strip() for x in args.tasks.split(",")}]

    for model_id in models:
        if spent >= budget:
            print(f"\nbudget reached (${spent:.4f}); stopping before {model_id}", flush=True)
            break
        already = {r["task"] for r in results["runs"].get(model_id, [])}
        todo = [t for t in tasks if t.id not in already]
        if not todo:
            continue
        print(f"\n=== {model_id} ===", flush=True)
        results["runs"].setdefault(model_id, [])
        for task in todo:
            remaining = budget - spent
            if remaining <= Decimal("0.20"):
                print(f"  {task.id}: skipped, ${remaining:.4f} left", flush=True)
                break
            result = run_task(client, model_id, task, prices[model_id], remaining)
            spent += Decimal(str(result.usd))
            results["runs"][model_id].append(asdict(result))
            save()
            rate = (result.malformed / result.total_calls * 100) if result.total_calls else 0.0
            print(
                f"  {task.id}: {result.steps_passed}/{result.steps_total} steps  "
                f"turns={result.turns:<3} calls={result.total_calls:<3} empty={result.empty_responses} "
                f"malformed={result.malformed} ({rate:.1f}%)  "
                f"missing_ref={result.missing_reference} "
                f"oos_edit={len(result.out_of_scope_edits)} "
                f"new={len(result.new_files_outside_scope)} "
                f"{result.seconds}s  ${result.usd:.4f}  [{result.stopped}]",
                flush=True,
            )
        print(f"  running total: ${spent:.4f} of ${budget}", flush=True)

    save()
    print(f"\ndone. spent ${spent:.4f}. results -> {RESULTS}")
    if results["not_reached"]:
        print("not reached:", ", ".join(results["not_reached"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
