#!/usr/bin/env python3
"""Fail if anything outside app/config.py reads the environment.

CLAUDE.md: "Env is read only in app/config.py." import-linter enforces the
import half of that rule but cannot see attribute access -- `os.environ` is not
a module -- so this covers the rest. Run by `make check`.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "orchestrator" / "app"
ALLOWED = {APP / "config.py"}

#: os.environ, os.getenv, os.environb, os.putenv -- every documented door.
ENV_ATTRS = {"environ", "environb", "getenv", "putenv", "unsetenv"}


def offences(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        # os.environ / os.getenv(...)
        if isinstance(node, ast.Attribute):
            value = node.value
            if isinstance(value, ast.Name) and value.id == "os" and node.attr in ENV_ATTRS:
                found.append((node.lineno, f"os.{node.attr}"))

        # from os import environ / getenv
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name in ENV_ATTRS:
                    found.append((node.lineno, f"from os import {alias.name}"))

    return found


def main() -> int:
    failures: list[str] = []

    for path in sorted(APP.rglob("*.py")):
        if path in ALLOWED:
            continue
        for lineno, what in offences(path):
            failures.append(f"{path.relative_to(ROOT)}:{lineno}: reads the environment via {what}")

    if failures:
        print("Environment is read only in app/config.py (CLAUDE.md). Found:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print(
            "\nTake a Settings instance instead, and add the field to app/config.py.",
            file=sys.stderr,
        )
        return 1

    print(f"env access: ok ({len(list(APP.rglob('*.py')))} files, only app/config.py reads env)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
