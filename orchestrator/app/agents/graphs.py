"""The agent graphs, and the registry the runtimes seam executes them through.

Each graph is small on purpose in V1: one model call, one validation step. The
interesting work -- tool use -- happens in the sandbox, never here. See the
security note in CLAUDE.md: agent tool calls do not run in the orchestrator.
"""

from __future__ import annotations

from typing import Any, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph

from app.agents.prompts import PromptLibrary
from app.models.base import Completion, Message, ModelClient, Role

log = structlog.get_logger(__name__)


class GraphState(TypedDict, total=False):
    run_id: str
    brief: str
    spec: str
    build_log: str
    test_report: str
    failure: str
    _idempotency_key: str
    _nodes_run: list[str]


class GraphRegistry:
    """Name -> compiled graph. Handed to the runtimes seam by the container."""

    def __init__(self, graphs: dict[str, Any]) -> None:
        self._graphs = graphs

    def get(self, name: str) -> Any | None:
        return self._graphs.get(name)

    def names(self) -> list[str]:
        return sorted(self._graphs)


def build_registry(
    *, models: ModelClient, prompts: PromptLibrary, timeout_seconds: float
) -> GraphRegistry:
    return GraphRegistry(
        {
            "specify": _single_call_graph(
                node="specify",
                role="spec",
                prompt="specify",
                output_key="spec",
                models=models,
                prompts=prompts,
                timeout_seconds=timeout_seconds,
            ),
            "build": _single_call_graph(
                node="build",
                role="build",
                prompt="build",
                output_key="build_log",
                models=models,
                prompts=prompts,
                timeout_seconds=timeout_seconds,
            ),
            "test": _single_call_graph(
                node="test",
                role="test",
                prompt="test",
                output_key="test_report",
                models=models,
                prompts=prompts,
                timeout_seconds=timeout_seconds,
            ),
        }
    )


def _single_call_graph(
    *,
    node: str,
    role: Role,
    prompt: str,
    output_key: str,
    models: ModelClient,
    prompts: PromptLibrary,
    timeout_seconds: float,
) -> Any:
    async def run_node(state: GraphState) -> GraphState:
        rendered = prompts.render(
            prompt,
            brief=state.get("brief", ""),
            spec=state.get("spec", ""),
            build_log=state.get("build_log", ""),
        )
        result = await models.complete(
            role=role,
            messages=[
                Message(role="system", content=prompts.get("system")),
                Message(role="user", content=rendered),
            ],
            timeout_seconds=timeout_seconds,
        )

        nodes_run = [*state.get("_nodes_run", []), node]
        if isinstance(result, Completion):
            log.info(
                "agent.node_completed",
                node=node,
                model=result.model_id,
                tokens=result.usage.total_tokens,
            )
            return {output_key: result.text, "_nodes_run": nodes_run}  # type: ignore[misc]

        # A model failure is a value all the way up: the node records it in the
        # state and the service decides whether it fails the run.
        log.warning("agent.node_failed", node=node, reason=result.reason, detail=result.detail)
        return {"failure": f"{result.reason}: {result.detail}", "_nodes_run": nodes_run}

    graph = StateGraph(GraphState)
    graph.add_node(node, run_node)
    graph.add_edge(START, node)
    graph.add_edge(node, END)
    return graph.compile()
