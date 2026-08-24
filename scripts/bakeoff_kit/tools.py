"""A minimal, hand-written tool loop.

Hand-written on purpose. LangGraph would sit between the model and the schema
and paper over exactly the failures this is here to measure -- a framework that
repairs a malformed call is a framework that hides which model produces them.

Six tools: read, write, edit, bash, glob, grep. Every call is validated against
its schema by hand, and every violation is counted rather than corrected.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: First token of a bash command must be one of these. A model that reaches for
#: anything else gets a refusal it can read, which is what a real sandbox does.
BASH_ALLOWLIST = frozenset({
    "python", "python3", "pytest", "make", "ls", "cat", "head", "tail",
    "grep", "rg", "find", "wc", "echo", "pwd", "git", "diff", "true", "sed", "sort", "uniq",
})

BASH_TIMEOUT_SECONDS = 90

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a file. Returns its contents with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path relative to the repository root."}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write a file, replacing it entirely. Creates parent directories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Replace an exact string in a file. old_string must appear exactly once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the repository root. Use it to run the build and the tests.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "List files matching a glob pattern, relative to the repository root.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents for a regular expression.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "Optional subdirectory or file to search."},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
]

_SCHEMAS = {t["function"]["name"]: t["function"]["parameters"] for t in TOOLS}


@dataclass
class ToolStats:
    """What the loop measures. Counters only; no judgement here."""

    total_calls: int = 0
    malformed: int = 0
    malformed_detail: list[str] = field(default_factory=list)
    missing_reference: int = 0
    missing_reference_detail: list[str] = field(default_factory=list)
    outside_repo_attempts: int = 0
    outside_repo_detail: list[str] = field(default_factory=list)
    refused_bash: int = 0
    #: (call index, was_malformed, was_missing_reference), for degradation.
    timeline: list[tuple[int, bool, bool]] = field(default_factory=list)


def validate(name: str, raw_arguments: str) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a tool call against its schema by hand.

    Returns (arguments, error). A non-None error is a schema violation, and is
    counted. Nothing is repaired: a call that does not conform is reported back
    to the model as an error, exactly as a strict runtime would.
    """
    if name not in _SCHEMAS:
        return None, f"unknown tool {name!r}; available: {', '.join(sorted(_SCHEMAS))}"

    try:
        arguments = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError as exc:
        return None, f"arguments are not valid JSON: {exc}"

    if not isinstance(arguments, dict):
        return None, "arguments must be a JSON object"

    schema = _SCHEMAS[name]
    properties: dict[str, Any] = schema["properties"]

    missing = [k for k in schema["required"] if k not in arguments]
    if missing:
        return None, f"missing required argument(s): {', '.join(missing)}"

    unknown = [k for k in arguments if k not in properties]
    if unknown:
        return None, f"unknown argument(s): {', '.join(unknown)}"

    for key, value in arguments.items():
        if properties[key]["type"] == "string" and not isinstance(value, str):
            return None, f"{key} must be a string, got {type(value).__name__}"

    return arguments, None


class Workspace:
    """The throwaway repo, and the only place a tool may touch."""

    def __init__(self, root: Path, stats: ToolStats) -> None:
        self.root = root.resolve()
        self.stats = stats

    def _resolve(self, raw: str) -> Path | None:
        """Resolve a path inside the repo, or None if it escapes."""
        candidate = (self.root / raw).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            self.stats.outside_repo_attempts += 1
            self.stats.outside_repo_detail.append(raw)
            return None
        return candidate

    def run(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Execute a validated call. Returns (result, referenced_something_missing)."""
        handler = getattr(self, f"_{name}")
        return handler(arguments)

    # --- tools ------------------------------------------------------------

    def _read(self, a: dict[str, Any]) -> tuple[str, bool]:
        path = self._resolve(a["path"])
        if path is None:
            return f"refused: {a['path']} is outside the repository", False
        if not path.is_file():
            return f"error: no such file: {a['path']}", True
        lines = path.read_text().splitlines()
        return "\n".join(f"{i:5d}\t{line}" for i, line in enumerate(lines, 1)), False

    def _write(self, a: dict[str, Any]) -> tuple[str, bool]:
        path = self._resolve(a["path"])
        if path is None:
            return f"refused: {a['path']} is outside the repository", False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(a["content"])
        return f"wrote {a['path']} ({len(a['content'])} bytes)", False

    def _edit(self, a: dict[str, Any]) -> tuple[str, bool]:
        path = self._resolve(a["path"])
        if path is None:
            return f"refused: {a['path']} is outside the repository", False
        if not path.is_file():
            return f"error: no such file: {a['path']}", True
        content = path.read_text()
        occurrences = content.count(a["old_string"])
        if occurrences == 0:
            # The model anchored on text that is not there -- a function or a
            # line it believes exists and does not.
            return f"error: old_string not found in {a['path']}", True
        if occurrences > 1:
            return f"error: old_string appears {occurrences} times in {a['path']}; make it unique", False
        path.write_text(content.replace(a["old_string"], a["new_string"], 1))
        return f"edited {a['path']}", False

    def _bash(self, a: dict[str, Any]) -> tuple[str, bool]:
        command = a["command"].strip()
        first = re.split(r"[\s;|&]+", command)[0] if command else ""
        if first not in BASH_ALLOWLIST:
            self.stats.refused_bash += 1
            return (
                f"refused: {first!r} is not permitted. Allowed: "
                f"{', '.join(sorted(BASH_ALLOWLIST))}"
            ), False
        try:
            done = subprocess.run(
                command, shell=True, cwd=self.root, capture_output=True,
                text=True, timeout=BASH_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return f"error: command timed out after {BASH_TIMEOUT_SECONDS}s", False
        out = (done.stdout + done.stderr).strip()
        return f"exit {done.returncode}\n{out[:6000]}", False

    def _glob(self, a: dict[str, Any]) -> tuple[str, bool]:
        matches = sorted(
            str(p.relative_to(self.root))
            for p in self.root.glob(a["pattern"])
            if ".git" not in p.parts
        )
        return "\n".join(matches) if matches else "(no matches)", not matches

    def _grep(self, a: dict[str, Any]) -> tuple[str, bool]:
        try:
            pattern = re.compile(a["pattern"])
        except re.error as exc:
            return f"error: bad regular expression: {exc}", False
        target = self._resolve(a.get("path", "."))
        if target is None:
            return f"refused: {a.get('path')} is outside the repository", False
        if not target.exists():
            return f"error: no such path: {a.get('path')}", True

        files = [target] if target.is_file() else [
            p for p in target.rglob("*") if p.is_file() and ".git" not in p.parts
        ]
        hits: list[str] = []
        for file in files:
            try:
                for number, line in enumerate(file.read_text().splitlines(), 1):
                    if pattern.search(line):
                        hits.append(f"{file.relative_to(self.root)}:{number}: {line.strip()[:200]}")
            except (UnicodeDecodeError, OSError):
                continue
        return "\n".join(hits[:100]) if hits else "(no matches)", False
