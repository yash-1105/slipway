"""runtimes seam: executing an agent graph.

V1 runs LangGraph in the worker process. V2 is a durable execution engine so a
graph survives a worker restart mid-node instead of replaying from its last
committed step. Callers hand over a graph name and an input, and get back
either a finished run or a failure describing where it stopped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class GraphInvocation:
    run_id: UUID
    graph: str
    inputs: dict[str, object]
    #: Retried invocations reuse this, so a graph can recognise its own replay.
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class GraphOutcome:
    outputs: dict[str, object]
    nodes_run: tuple[str, ...]
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class GraphFailure:
    reason: Literal["node_failed", "timeout", "cancelled", "budget_exhausted", "unknown_graph"]
    detail: str
    #: The node that stopped, so the runbook can say where to look.
    failed_node: str | None = None
    nodes_run: tuple[str, ...] = field(default_factory=tuple)


GraphResult = GraphOutcome | GraphFailure


class GraphRuntime(Protocol):
    """The runtimes seam."""

    async def invoke(self, invocation: GraphInvocation, *, timeout_seconds: float) -> GraphResult:
        """Run a graph to completion. Failures come back as values."""

    async def cancel(self, run_id: UUID) -> None:
        """Ask a running graph to stop at its next node boundary. Idempotent."""
