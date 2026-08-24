"""models seam: talking to an LLM.

Callers pass a logical *role* -- "planner", "builder", "evaluator" -- never a
model id. Roles resolve to ids through config/models.yaml, which is generated
from the live /models endpoint. No model id is typed from memory anywhere.

Novita implements chat completions only; it has no Responses API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

#: The logical roles a caller may ask for. A caller names one of these; it never
#: names a model. Which model a role resolves to -- and which model it falls
#: back to -- is app/models/router.py reading config/models.yaml.
#:
#: "spec" is legacy and is not configured in config/models.yaml. It remains in
#: the union solely so app/models/impl/novita.py, which is finished and tested,
#: continues to type-check without being edited: its `ping()` names that role.
#: The router's own `ping()` uses "planner", so the legacy path is never taken.
Role = Literal[
    "planner",
    "builder",
    "evaluator",
    "test_author",
    "doc_writer",
    "spec",
]


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class Completion:
    """A successful completion."""

    text: str
    model_id: str
    usage: Usage
    finish_reason: str


@dataclass(frozen=True, slots=True)
class CompletionFailure:
    """A completion that did not happen, as a value.

    The caller has to decide what to do about a refusal, a context overflow or
    an exhausted budget, so these are returned rather than raised.
    """

    reason: Literal["timeout", "rate_limited", "provider_error", "budget_exhausted", "refused"]
    detail: str
    model_id: str | None = None
    #: True when retrying the identical request could plausibly succeed.
    retryable: bool = False
    attempts: int = 1


CompletionResult = Completion | CompletionFailure


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One row of config/models.yaml."""

    role: str
    model_id: str
    context_window: int
    max_output_tokens: int
    notes: str = ""


@dataclass(frozen=True, slots=True)
class ModelCatalogue:
    """The contents of config/models.yaml.

    `source_base_url` and `synced_at` record which endpoint the ids came from,
    so a stale catalogue is visible rather than mysterious.
    """

    source_base_url: str
    synced_at: str
    by_role: dict[str, ModelSpec] = field(default_factory=dict)

    def resolve(self, role: str) -> ModelSpec | None:
        return self.by_role.get(role)


class ModelClient(Protocol):
    """The models seam."""

    async def complete(
        self,
        *,
        role: Role,
        messages: list[Message],
        timeout_seconds: float,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> CompletionResult:
        """Run one chat completion. Never raises for a provider-side failure."""

    async def ping(self, *, timeout_seconds: float) -> CompletionResult:
        """One real, minimal call. Used by `slipway models ping`."""
