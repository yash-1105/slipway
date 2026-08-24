"""models seam: a deterministic in-process client.

Used by tests/unit and by `make dev` before a Novita key is configured, so the
loop is exercisable without spending money. It is a real implementation of the
Protocol, not a mock: tests assert on what comes back, not on what was called.
"""

from __future__ import annotations

import hashlib

from app.models.base import Completion, CompletionResult, Message, ModelCatalogue, Role, Usage


class FakeModelClient:
    def __init__(self, *, catalogue: ModelCatalogue, canned: dict[str, str] | None = None) -> None:
        self._catalogue = catalogue
        self._canned = canned or {}

    async def complete(
        self,
        *,
        role: Role,
        messages: list[Message],
        timeout_seconds: float,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> CompletionResult:
        prompt = "\n".join(m.content for m in messages)
        digest = hashlib.sha256(prompt.encode()).hexdigest()[:12]
        text = self._canned.get(role, f"[fake:{role}:{digest}]")
        spec = self._catalogue.resolve(role)
        return Completion(
            text=text,
            model_id=spec.model_id if spec else f"fake/{role}",
            usage=Usage(prompt_tokens=len(prompt) // 4, completion_tokens=len(text) // 4),
            finish_reason="stop",
        )

    async def ping(self, *, timeout_seconds: float) -> CompletionResult:
        return await self.complete(
            role="spec",
            messages=[Message(role="user", content="ping")],
            timeout_seconds=timeout_seconds,
        )
