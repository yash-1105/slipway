"""The four tasks, and the checks that decide whether each was done.

Every verdict is mechanical. "The model did a good job" is not a measurement;
"the suite passes and the new test names a leap day" is.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from bakeoff_kit.fixture import BASE, broken_import


def _run(root: Path, command: str, timeout: int = 120) -> tuple[int, str]:
    try:
        done = subprocess.run(
            command, shell=True, cwd=root, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    return done.returncode, done.stdout + done.stderr


def _suite_passes(root: Path) -> bool:
    return _run(root, "python -m pytest -q")[0] == 0


def _text(root: Path, relative: str) -> str:
    path = root / relative
    return path.read_text() if path.is_file() else ""


@dataclass
class Step:
    """One instruction, and the check that says whether it landed."""

    prompt: str
    check: Callable[[Path], bool]
    label: str


@dataclass
class Task:
    id: str
    title: str
    files: dict[str, str]
    steps: list[Step]
    #: Paths the task legitimately touches. Anything else modified is reported.
    in_scope: list[str] = field(default_factory=list)


# --- T1 -------------------------------------------------------------------


def _t1_check(root: Path) -> bool:
    tests = _text(root, "tests/test_dates.py")
    # A new test that actually names a leap day, and a green suite.
    names_leap_day = bool(re.search(r"02-29|2, 29|Feb(ruary)? 29|leap", tests, re.I))
    added_a_test = tests.count("def test_") > BASE["tests/test_dates.py"].count("def test_")
    return names_leap_day and added_a_test and _suite_passes(root)


T1 = Task(
    id="T1",
    title="Read three files, add a failing leap-year test, make it pass",
    files=dict(BASE),
    in_scope=["tests/test_dates.py", "src/acme/dates.py"],
    steps=[
        Step(
            label="leap-year test added and green",
            check=_t1_check,
            prompt=(
                "Read src/acme/dates.py, src/acme/validation.py and tests/test_dates.py.\n\n"
                "Find the function that parses dates. Add a unit test to tests/test_dates.py "
                "for a leap-year edge case that the current tests do not cover -- a real "
                "February 29th. Write the test first and run it; if it already passes, choose "
                "an edge case that genuinely does not, such as a February 29th in a year that "
                "is not a leap year being rejected.\n\n"
                "Then make the whole suite pass with `make test`. Do not change unrelated files."
            ),
        )
    ],
)


# --- T2 -------------------------------------------------------------------


def _t2_check(root: Path) -> bool:
    return _run(root, "make build")[0] == 0 and "import json" in _text(root, "src/acme/routes.py")


T2 = Task(
    id="T2",
    title="Broken build from a missing import: run it, read the error, fix it",
    files=broken_import(),
    in_scope=["src/acme/routes.py"],
    steps=[
        Step(
            label="build green and the import restored",
            check=_t2_check,
            prompt=(
                "The build is broken. Run `make build`, read the error output, find the cause "
                "and fix it. Then run `make build` again to confirm it is green.\n\n"
                "Fix the actual cause. Do not delete the code that fails, and do not change "
                "anything the error does not point at."
            ),
        )
    ],
)


# --- T3 -------------------------------------------------------------------


def _t3_check(root: Path) -> bool:
    routes = _text(root, "src/acme/routes.py")
    validation = _text(root, "src/acme/validation.py")
    tests = _text(root, "tests/test_routes.py")

    has_route = '"/api/notes"' in routes and "POST" in routes
    # The convention: validate via a validate_* function, not inline.
    has_validator = bool(re.search(r"def validate_\w*body|def validate_\w+", validation)) and (
        validation.count("def validate_") > BASE["src/acme/validation.py"].count("def validate_")
    )
    uses_validator = bool(re.search(r"validate_\w+\(payload\)", routes))
    has_test = tests.count("def test_") > BASE["tests/test_routes.py"].count("def test_")
    return has_route and has_validator and uses_validator and has_test and _suite_passes(root)


T3 = Task(
    id="T3",
    title="Add a route with validation and a test, following repo conventions",
    files=dict(BASE),
    in_scope=["src/acme/routes.py", "src/acme/validation.py", "tests/test_routes.py"],
    steps=[
        Step(
            label="route, validator and test present, suite green",
            check=_t3_check,
            prompt=(
                "Add a POST route at /api/notes that accepts a payload with a `name` and a "
                "`body`. `body` must be a non-empty string of at most 500 characters.\n\n"
                "Follow the conventions already in this repository -- read README.md and the "
                "existing routes first. Add its test alongside the existing route tests.\n\n"
                "`make build` must be green when you are done."
            ),
        )
    ],
)


# --- T4 -------------------------------------------------------------------


def _t4_1(root: Path) -> bool:
    helpers = _text(root, "src/acme/helpers.py")
    if "def slugify" not in helpers:
        return False
    code, out = _run(
        root,
        "python -c \"import sys; sys.path.insert(0,'src'); from acme.helpers import slugify; "
        "print(slugify('Hello World  Again'))\"",
    )
    return code == 0 and "hello-world-again" in out.strip().lower()


def _t4_2(root: Path) -> bool:
    return "slugify" in _text(root, "src/acme/routes.py")


def _t4_3(root: Path) -> bool:
    validation = _text(root, "src/acme/validation.py")
    return "validate_tags" in validation and "ValidationError" in validation


def _t4_4(root: Path) -> bool:
    tests = _text(root, "tests/test_routes.py")
    return "slug" in tests.lower() and _suite_passes(root)


def _t4_5(root: Path) -> bool:
    validation = _text(root, "src/acme/validation.py")
    # A shared string-field validator, used by more than one validator.
    shared = re.search(r"def (_?\w*(string|field|text)\w*)\(", validation)
    if shared is None:
        return False
    return validation.count(shared.group(1)) >= 3 and _suite_passes(root)


def _t4_6(root: Path) -> bool:
    changelog = _text(root, "CHANGELOG.md")
    return bool(changelog.strip()) and "slug" in changelog.lower()


T4 = Task(
    id="T4",
    title="Six dependent tasks in one session, to measure degradation",
    files=dict(BASE),
    in_scope=[
        "src/acme/helpers.py", "src/acme/routes.py", "src/acme/validation.py",
        "tests/test_routes.py", "CHANGELOG.md",
    ],
    steps=[
        Step("1: slugify in helpers", _t4_1,
             "Add a `slugify(text)` function to src/acme/helpers.py. It lowercases, replaces "
             "runs of whitespace with a single hyphen, and strips anything that is not a "
             "letter, a digit or a hyphen. `slugify('Hello World  Again')` must return "
             "'hello-world-again'. Follow the style of the existing helper."),
        Step("2: route uses it", _t4_2,
             "Use slugify in src/acme/routes.py so that creating a task stores a `slug` "
             "derived from its name, alongside the existing fields."),
        Step("3: tags validator", _t4_3,
             "Add a `validate_tags` function to src/acme/validation.py. `tags` is optional; "
             "when present it must be a list of non-empty strings, at most 10 of them. Raise "
             "ValidationError naming the field, like the other validators. Use it in the "
             "create-task route."),
        Step("4: test the slug", _t4_4,
             "Add a test to tests/test_routes.py that creates a task and asserts the stored "
             "slug is what slugify produces. Run the suite and make it green."),
        Step("5: shared validation", _t4_5,
             "The validators in src/acme/validation.py now repeat the same "
             "non-empty-string-with-a-length-limit check. Extract it into one shared function "
             "and have the others call it. Behaviour must not change; the suite must stay green."),
        Step("6: changelog", _t4_6,
             "Add a CHANGELOG.md at the repository root describing, in a short bulleted list, "
             "everything changed in this session. Then run `make build` one last time and "
             "confirm it is green."),
    ],
)


TASKS: list[Task] = [T1, T2, T3, T4]
