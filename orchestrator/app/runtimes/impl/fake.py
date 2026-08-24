"""runtimes seam: a fake that returns canned graph outcomes."""

from __future__ import annotations

from uuid import UUID

from app.runtimes.base import (
    GraphFailure,
    GraphInvocation,
    GraphOutcome,
    GraphResult,
    GraphRuntime,
)


class FakeRuntime(GraphRuntime):
    def __init__(self, *, outcomes: dict[str, GraphResult] | None = None) -> None:
        self._outcomes = outcomes or {}
        self._cancelled: set[UUID] = set()

    async def invoke(self, invocation: GraphInvocation, *, timeout_seconds: float) -> GraphResult:
        if invocation.run_id in self._cancelled:
            return GraphFailure("cancelled", "cancelled")
        return self._outcomes.get(
            invocation.graph,
            GraphOutcome(
                outputs={"graph": invocation.graph, **invocation.inputs},
                nodes_run=(invocation.graph,),
                duration_seconds=0.0,
            ),
        )

    async def cancel(self, run_id: UUID) -> None:
        self._cancelled.add(run_id)
