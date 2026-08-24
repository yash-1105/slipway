"""No caller anywhere passes a model string.

CLAUDE.md: "Never type a model id from memory. Read it from config/models.yaml."
The rule only holds if it is impossible to break quietly, so this checks two
things statically:

1. The seam's own signature. `ModelClient.complete` takes a `role`, and has no
   parameter a model id could be passed through. That makes the common case
   structurally impossible rather than merely discouraged.
2. Every call site. Nothing outside `app/models/` names a model in an argument,
   and every `.complete(...)` names a role.

An import-linter contract cannot express this: contracts reason about which
modules import which, and a model id is a string, not an import.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.models.base import ModelClient
from app.models.router import ROUTER_ROLES

APP = Path(__file__).resolve().parents[2] / "app"

#: The seam owns model ids. Everything else asks for a role.
SEAM = APP / "models"

#: Keyword names through which a model id could reach a provider.
_MODEL_KWARGS = frozenset({"model", "model_id", "model_name", "engine", "deployment"})


def _modules_outside_the_seam() -> list[Path]:
    return sorted(p for p in APP.rglob("*.py") if SEAM not in p.parents and p.parent != SEAM)


def test_the_seam_signature_has_no_way_to_pass_a_model() -> None:
    parameters = set(inspect.signature(ModelClient.complete).parameters)

    assert "role" in parameters
    assert not (parameters & _MODEL_KWARGS), (
        "ModelClient.complete accepts a parameter a model id could be passed "
        "through. Callers name a role; the seam resolves it."
    )


def test_the_role_vocabulary_is_the_five_routed_roles() -> None:
    assert ROUTER_ROLES == ("planner", "builder", "evaluator", "test_author", "doc_writer")


@pytest.mark.parametrize("path", _modules_outside_the_seam(), ids=lambda p: str(p.name))
def test_no_module_outside_the_seam_passes_a_model_argument(path: Path) -> None:
    tree = ast.parse(path.read_text(), filename=str(path))

    offences: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg not in _MODEL_KWARGS:
                continue
            # A *literal* is a model id typed from memory. Forwarding a value
            # read off a response -- `model=result.model_id` in a log line, or
            # into a ledger row -- is reporting what happened, which is the
            # opposite of the thing being banned and is required elsewhere.
            if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                offences.append(
                    f"{path.name}:{node.lineno}: {keyword.arg}={keyword.value.value!r}"
                )
            # Naming one on the way into a completion is banned however it got
            # there: the seam resolves the model, callers do not choose it.
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"complete", "create"}:
                offences.append(f"{path.name}:{node.lineno}: passes {keyword.arg}= to a completion")

    assert not offences, (
        f"{offences}. Only app/models/ names a model; everywhere else asks for a role."
    )


@pytest.mark.parametrize("path", _modules_outside_the_seam(), ids=lambda p: str(p.name))
def test_every_completion_call_names_a_role(path: Path) -> None:
    tree = ast.parse(path.read_text(), filename=str(path))

    offences: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "complete"):
            continue
        if not any(keyword.arg == "role" for keyword in node.keywords):
            offences.append(f"{path.name}:{node.lineno}: .complete() without role=")

    assert not offences, offences


def test_no_module_outside_the_seam_contains_a_provider_model_id() -> None:
    """A vendor-shaped id in source is one nobody read from config/models.yaml.

    Matches the `vendor/model` shape provider ids use. A false positive here is
    a string that should probably not be a literal anyway.
    """
    import re

    # `something/something-with-a-digit`, the shape of a hosted model id.
    pattern = re.compile(r"\b[a-z0-9][\w.-]*/[a-z0-9][\w.-]*\d[\w.-]*\b")
    allowed = {"tests", "migrations"}

    offences: list[str] = []
    for path in _modules_outside_the_seam():
        if allowed & set(path.parts):
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("--"):
                continue
            for match in pattern.finditer(line):
                # Paths and URLs are not model ids.
                if any(marker in line for marker in ("http", "://", "import ", "docs/", ".py")):
                    continue
                offences.append(f"{path.name}:{number}: {match.group(0)!r}")

    assert not offences, (
        f"{offences}. These look like provider model ids. Read them from "
        "config/models.yaml through app/models/router.py."
    )
