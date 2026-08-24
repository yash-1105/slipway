"""sandbox seam: where agent tool calls run.

Agent tool calls never execute in the orchestrator process. They execute in a
sandbox, always. V1 is a Docker container; see ARCHITECTURE.md.

The handle is recorded in the database *before* the sandbox is created, so a
crash between "we decided the name" and "the container exists" is reconcilable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class SandboxHandle:
    """An opaque reference to a sandbox. `name` is deterministic per run."""

    run_id: UUID
    name: str
    backend: str


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    image: str
    workspace_host_path: str
    cpu_limit: float
    memory_limit_mb: int
    #: Hosts the sandbox may reach. Everything else is refused.
    network_allowlist: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExecResult:
    """The outcome of one command in the sandbox, as a value.

    A non-zero exit code is data, not an exception: an agent needs to read the
    compiler's complaint, not catch it.
    """

    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True, slots=True)
class SandboxFailure:
    reason: Literal["create_failed", "not_found", "image_missing", "resource_exhausted"]
    detail: str


StartResult = SandboxHandle | SandboxFailure


class Sandbox(Protocol):
    """The sandbox seam."""

    async def start(
        self, handle: SandboxHandle, spec: SandboxSpec, *, timeout_seconds: float
    ) -> StartResult:
        """Create the sandbox for an already-recorded handle. Idempotent."""

    async def exec(
        self, handle: SandboxHandle, command: list[str], *, timeout_seconds: float
    ) -> ExecResult:
        """Run one command. A timeout returns `timed_out=True`, it does not raise."""

    async def write_file(
        self, handle: SandboxHandle, path: str, content: bytes, *, timeout_seconds: float
    ) -> None: ...

    async def read_file(
        self, handle: SandboxHandle, path: str, *, timeout_seconds: float
    ) -> bytes | None: ...

    async def stop(self, handle: SandboxHandle, *, timeout_seconds: float) -> None:
        """Tear down. Idempotent; stopping a sandbox that is gone is success."""

    async def list_live(self) -> list[SandboxHandle]:
        """What actually exists, for the reconciliation loop to compare against."""
