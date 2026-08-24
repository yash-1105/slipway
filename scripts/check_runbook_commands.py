#!/usr/bin/env python3
"""Fail if RUNBOOK.md tells an operator to run a command that does not exist.

CLAUDE.md: "A command named in a runbook must exist." An operator follows this
document during an incident, at an unpleasant hour, and a command that is not
there costs them the time they least have.

Only *prescriptions* are checked -- `slipway ...` inside an indented or fenced
code block, which is what the document is telling you to type. Prose is not:
the runbook deliberately says things like "there is no `slipway runs unstick`",
and a check that could not tell those apart would force the document to stay
silent about its own gaps, which is worse than the gap.

Run by `make check`, alongside scripts/check_env_access.py.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "RUNBOOK.md"

sys.path.insert(0, str(ROOT / "orchestrator"))


def known_commands() -> set[str]:
    """Every invocable command path, from the CLI itself rather than a list."""
    from app.cli.main import app

    known: set[str] = set()
    for command in app.registered_commands:
        known.add(command.name or command.callback.__name__)
    for group in app.registered_groups:
        for command in group.typer_instance.registered_commands:
            leaf = command.name or command.callback.__name__
            known.add(f"{group.name} {leaf}")
    return known


def prescribed_commands(text: str) -> list[tuple[int, str]]:
    """(line number, command path) for every `slipway ...` inside a code block."""
    found: list[tuple[int, str]] = []
    in_fence = False

    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue

        indented = line.startswith("    ") and line.strip()
        if not (in_fence or indented):
            continue

        match = re.search(r"\bslipway\s+([a-z][\w-]*)(?:\s+([a-z][\w-]*))?", line)
        if match is None:
            continue

        group, leaf = match.group(1), match.group(2)
        # `slipway runs show <id>` -> "runs show"; `slipway migrate` -> "migrate".
        # A second word that is an argument or a flag is not part of the path.
        if leaf is None or leaf.startswith("-") or leaf.startswith("<"):
            found.append((number, group))
        else:
            found.append((number, f"{group} {leaf}"))

    return found


def main() -> int:
    text = RUNBOOK.read_text()
    known = known_commands()

    prescribed = prescribed_commands(text)
    failures: list[str] = []

    for number, path in prescribed:
        if path in known:
            continue
        # A one-word path may be a group used with a subcommand this parser
        # split off; accept it if the group exists at all.
        if " " not in path and any(k.startswith(f"{path} ") for k in known):
            continue
        failures.append(f"RUNBOOK.md:{number}: prescribes `slipway {path}`, which does not exist")

    if failures:
        print("RUNBOOK.md prescribes commands the CLI does not have:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print(
            "\nEither build the command, or move it to the 'Not built yet' table "
            "and give the procedure something that works today.",
            file=sys.stderr,
        )
        return 1

    print(
        f"runbook: ok ({len(prescribed)} prescribed invocations, "
        f"{len(known)} commands available)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
