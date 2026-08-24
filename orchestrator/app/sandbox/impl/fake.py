"""sandbox seam: an in-process fake with a real filesystem model.

Satisfies the Protocol without Docker, for tests/unit and for `make dev`.
Commands are not executed; a canned table maps argv to an ExecResult so a test
can assert on what the agent did with the output.
"""

from __future__ import annotations

from app.sandbox.base import (
    ExecResult,
    Sandbox,
    SandboxHandle,
    SandboxSpec,
    StartResult,
)


class FakeSandbox(Sandbox):
    def __init__(self, *, canned: dict[str, ExecResult] | None = None) -> None:
        self._canned = canned or {}
        self._live: dict[str, SandboxHandle] = {}
        self._files: dict[tuple[str, str], bytes] = {}

    async def start(
        self, handle: SandboxHandle, spec: SandboxSpec, *, timeout_seconds: float
    ) -> StartResult:
        self._live[handle.name] = handle
        return handle

    async def exec(
        self, handle: SandboxHandle, command: list[str], *, timeout_seconds: float
    ) -> ExecResult:
        key = " ".join(command)
        return self._canned.get(
            key, ExecResult(exit_code=0, stdout="", stderr="", duration_seconds=0.0)
        )

    async def write_file(
        self, handle: SandboxHandle, path: str, content: bytes, *, timeout_seconds: float
    ) -> None:
        self._files[(handle.name, path)] = content

    async def read_file(
        self, handle: SandboxHandle, path: str, *, timeout_seconds: float
    ) -> bytes | None:
        return self._files.get((handle.name, path))

    async def stop(self, handle: SandboxHandle, *, timeout_seconds: float) -> None:
        self._live.pop(handle.name, None)

    async def list_live(self) -> list[SandboxHandle]:
        return list(self._live.values())
