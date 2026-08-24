"""The throwaway project the bake-off tasks operate on.

One dict, so a run can materialise a fresh git repo per (model, task) with no
shared state. Small, but real: it has conventions a model has to notice, a
working test suite, and a build command that can actually break.
"""

from __future__ import annotations

BASE: dict[str, str] = {
    # Without this, running the tests leaves __pycache__ directories that
    # `git status` reports, and every model looks like it edited files outside
    # the task. The metric has to measure what the model changed, not what
    # pytest left behind.
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "README.md": """# acme

A tiny internal service. Conventions:

- Routes live in `src/acme/routes.py`, registered with the `@route` decorator.
- Every route validates its payload with a `validate_*` function from
  `src/acme/validation.py`, which raises `ValidationError`.
- Every route has a test in `tests/test_routes.py`.
- `make build` must pass before anything is merged.
""",
    "Makefile": """.PHONY: build test
build:
\tpython -c "import sys; sys.path.insert(0, 'src'); import acme.routes"
\t$(MAKE) test

test:
\tpython -m pytest -q
""",
    "conftest.py": """import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
""",
    "src/acme/__init__.py": "",
    "src/acme/dates.py": '''"""Date handling."""

from __future__ import annotations


def is_leap_year(year: int) -> bool:
    """Gregorian leap year rule."""
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def days_in_month(year: int, month: int) -> int:
    """Number of days in a month, accounting for leap years."""
    if month < 1 or month > 12:
        raise ValueError(f"month out of range: {month}")
    lengths = [31, 29 if is_leap_year(year) else 28, 31, 30, 31, 30,
               31, 31, 30, 31, 30, 31]
    return lengths[month - 1]


def parse_iso_date(text: str) -> tuple[int, int, int]:
    """Parse YYYY-MM-DD into (year, month, day).

    Raises ValueError on anything that is not a real calendar date.
    """
    parts = text.split("-")
    if len(parts) != 3:
        raise ValueError(f"not an ISO date: {text!r}")
    try:
        year, month, day = (int(p) for p in parts)
    except ValueError as exc:
        raise ValueError(f"not an ISO date: {text!r}") from exc

    if day < 1 or day > days_in_month(year, month):
        raise ValueError(f"day out of range for month: {text!r}")
    return (year, month, day)
''',
    "src/acme/validation.py": '''"""Payload validation.

Every validator raises ValidationError with a message naming the field.
Routes never validate inline; they call one of these.
"""

from __future__ import annotations

from acme.dates import parse_iso_date


class ValidationError(Exception):
    """A payload the caller must fix."""


def validate_name(payload: dict) -> str:
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("name: required, must be a non-empty string")
    if len(name) > 80:
        raise ValidationError("name: must be 80 characters or fewer")
    return name.strip()


def validate_due_date(payload: dict) -> str:
    raw = payload.get("due_date")
    if not isinstance(raw, str):
        raise ValidationError("due_date: required, must be a string")
    try:
        parse_iso_date(raw)
    except ValueError as exc:
        raise ValidationError(f"due_date: {exc}") from exc
    return raw
''',
    "src/acme/store.py": '''"""In-memory store. No database in this service."""

from __future__ import annotations

_ITEMS: dict[str, dict] = {}


def put(key: str, value: dict) -> dict:
    _ITEMS[key] = value
    return value


def get(key: str) -> dict | None:
    return _ITEMS.get(key)


def all_items() -> dict[str, dict]:
    return dict(_ITEMS)


def clear() -> None:
    _ITEMS.clear()
''',
    "src/acme/helpers.py": '''"""Small shared helpers."""

from __future__ import annotations


def truncate(text: str, limit: int) -> str:
    """Shorten text to `limit` characters, appending an ellipsis if cut."""
    if limit < 1:
        raise ValueError("limit must be positive")
    return text if len(text) <= limit else text[: limit - 1] + "\\u2026"
''',
    "src/acme/routes.py": '''"""HTTP routes.

Each route is registered with @route(path, method) and validates its payload
with a validate_* function from acme.validation. Routes return a dict, which
the caller serialises.
"""

from __future__ import annotations

import json

from acme import store
from acme.validation import ValidationError, validate_due_date, validate_name

_ROUTES: dict[tuple[str, str], object] = {}


def route(path: str, method: str = "GET"):
    """Register a handler for (path, method)."""

    def register(handler):
        _ROUTES[(path, method.upper())] = handler
        return handler

    return register


def dispatch(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    """Call the handler for (path, method). Returns a dict with a status."""
    handler = _ROUTES.get((path, method.upper()))
    if handler is None:
        return {"status": 404, "error": f"no route for {method} {path}"}
    try:
        return handler(payload or {})
    except ValidationError as exc:
        return {"status": 400, "error": str(exc)}


@route("/api/tasks", "POST")
def create_task(payload: dict) -> dict:
    name = validate_name(payload)
    due_date = validate_due_date(payload)
    item = {"name": name, "due_date": due_date}
    store.put(name, item)
    return {"status": 201, "body": item}


@route("/api/tasks", "GET")
def list_tasks(payload: dict) -> dict:
    return {"status": 200, "body": {"items": list(store.all_items().values())}}


@route("/api/health", "GET")
def health(payload: dict) -> dict:
    return {"status": 200, "body": json.loads('{"ok": true}')}
''',
    "tests/test_dates.py": '''import pytest

from acme.dates import days_in_month, is_leap_year, parse_iso_date


def test_ordinary_year_february():
    assert days_in_month(2023, 2) == 28


def test_parses_a_normal_date():
    assert parse_iso_date("2023-03-14") == (2023, 3, 14)


def test_rejects_a_day_past_the_end_of_the_month():
    with pytest.raises(ValueError):
        parse_iso_date("2023-04-31")


def test_rejects_nonsense():
    with pytest.raises(ValueError):
        parse_iso_date("not-a-date")


def test_leap_year_rule_basics():
    assert is_leap_year(2024)
    assert not is_leap_year(2023)
''',
    "tests/test_routes.py": '''from acme import store
from acme.routes import dispatch


def setup_function():
    store.clear()


def test_create_task_accepts_a_valid_payload():
    result = dispatch("/api/tasks", "POST", {"name": "write it up", "due_date": "2024-02-29"})
    assert result["status"] == 201
    assert result["body"]["name"] == "write it up"


def test_create_task_rejects_a_blank_name():
    result = dispatch("/api/tasks", "POST", {"name": "  ", "due_date": "2024-01-01"})
    assert result["status"] == 400
    assert "name" in result["error"]


def test_create_task_rejects_a_bad_date():
    result = dispatch("/api/tasks", "POST", {"name": "x", "due_date": "2023-02-30"})
    assert result["status"] == 400
    assert "due_date" in result["error"]


def test_unknown_route_is_404():
    assert dispatch("/api/nope")["status"] == 404
''',
}


def broken_import() -> dict[str, str]:
    """The T2 variant: `import json` removed from routes.py.

    Breaks at import time -- `make build` does `import acme.routes` first -- so
    the model has to read the error rather than guess from the test output.
    """
    files = dict(BASE)
    files["src/acme/routes.py"] = files["src/acme/routes.py"].replace(
        "from __future__ import annotations\n\nimport json\n",
        "from __future__ import annotations\n",
    )
    files["src/acme/routes.py"] = files["src/acme/routes.py"].replace(
        '@route("/api/health", "GET")\ndef health(payload: dict) -> dict:\n'
        '    return {"status": 200, "body": json.loads(\'{"ok": true}\')}',
        '@route("/api/health", "GET")\ndef health(payload: dict) -> dict:\n'
        '    return {"status": 200, "body": json.loads(\'{"ok": true}\')}\n\n\n'
        "SCHEMA = json.dumps({\"task\": [\"name\", \"due_date\"]})",
    )
    return files
