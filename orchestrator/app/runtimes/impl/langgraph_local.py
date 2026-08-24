"""runtimes seam: LangGraph executed in the worker process.

The graph registry is passed in by the composition root rather than imported,
so this module does not depend on app/agents and the seam stays a leaf.
"""

from __future__ import annotations

import asyncio
from typing import Protocol
from uuid import UUID

import structlog

from app.runtimes.base import (
    GraphFailure,
    GraphInvocation,
    GraphOutcome,
    GraphResult,
    GraphRuntime,
)

log = structlog.get_logger(__name__)


class CompiledGraph(Protocol):
    """What app/agents hands us: anything LangGraph-compiled will do."""

    async def ainvoke(self, inputs: dict[str, object]) -> dict[str, object]: ...


class GraphRegistry(Protocol):
    def get(self, name: str) -> CompiledGraph | None: ...

    def names(self) -> list[str]: ...


class LocalLangGraphRuntime(GraphRuntime):
    def __init__(self, *, graphs: object) -> None:
        # Structurally typed: the registry is duck-checked here rather than
        # imported, because importing app.agents would invert the layering.
        self._graphs: GraphRegistry = graphs  # type: ignore[assignment]
        self._cancelled: set[UUID] = set()

    async def invoke(self, invocation: GraphInvocation, *, timeout_seconds: float) -> GraphResult:
        graph = self._graphs.get(invocation.graph)
        if graph is None:
            return GraphFailure(
                "unknown_graph",
                f"{invocation.graph!r} is not registered; known: {self._graphs.names()}",
            )

        if invocation.run_id in self._cancelled:
            self._cancelled.discard(invocation.run_id)
            return GraphFailure("cancelled", "cancelled before the first node ran")

        loop = asyncio.get_running_loop()
        started = loop.time()
        inputs = dict(invocation.inputs)
        inputs["_idempotency_key"] = invocation.idempotency_key

        try:
            outputs = await asyncio.wait_for(graph.ainvoke(inputs), timeout=timeout_seconds)
        except TimeoutError:
            return GraphFailure(
                "timeout", f"graph {invocation.graph!r} exceeded {timeout_seconds}s"
            )
        except asyncio.CancelledError:
            # Cooperative cancellation: the worker is shutting down. Re-raise
            # so the event loop can finish tearing down, after recording it.
            log.info("runtime.cancelled", run_id=str(invocation.run_id), graph=invocation.graph)
            raise

        nodes_raw = outputs.get("_nodes_run")
        nodes_run = tuple(str(n) for n in nodes_raw) if isinstance(nodes_raw, list) else ()
        return GraphOutcome(
            outputs=outputs,
            nodes_run=nodes_run,
            duration_seconds=loop.time() - started,
        )

    async def cancel(self, run_id: UUID) -> None:
        self._cancelled.add(run_id)
