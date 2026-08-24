"""models seam: Novita, via the OpenAI SDK's chat completions API.

Novita has no Responses API. Chat completions only -- see
docs/decisions/0004-novita-base-url.md, which also tracks the base URL, since
Novita's docs show three candidates and only one of them works.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any

import openai
import structlog

from app.models.base import (
    Completion,
    CompletionFailure,
    CompletionResult,
    Message,
    ModelCatalogue,
    Role,
    Usage,
)

log = structlog.get_logger(__name__)

#: Status codes worth trying again. Everything else is our fault or permanent.
_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class NovitaModelClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        catalogue: ModelCatalogue,
        default_timeout_seconds: float,
        max_attempts: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._catalogue = catalogue
        self._default_timeout = default_timeout_seconds
        self._max_attempts = max_attempts
        # Injectable so tests can assert on the backoff schedule without
        # actually waiting it out.
        self._sleep = sleep
        # max_retries=0: the SDK's own retry loop has no visibility into our
        # budget or our timeout policy, so we do the retrying ourselves.
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=default_timeout_seconds,
            max_retries=0,
        )

    async def complete(
        self,
        *,
        role: Role,
        messages: list[Message],
        timeout_seconds: float,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> CompletionResult:
        spec = self._catalogue.resolve(role)
        if spec is None:
            return CompletionFailure(
                reason="provider_error",
                detail=(
                    f"no model configured for role {role!r} in config/models.yaml "
                    f"(catalogue synced {self._catalogue.synced_at} from "
                    f"{self._catalogue.source_base_url}); run `make models-sync`"
                ),
                retryable=False,
            )

        payload: dict[str, Any] = {
            "model": spec.model_id,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_output_tokens or spec.max_output_tokens,
        }
        if temperature is not None:
            payload["temperature"] = temperature

        last: CompletionFailure | None = None
        for attempt in range(1, self._max_attempts + 1):
            outcome = await self._attempt(payload, spec.model_id, timeout_seconds, attempt)
            if isinstance(outcome, Completion):
                return outcome
            last = outcome
            if not outcome.retryable or attempt == self._max_attempts:
                break
            await self._sleep(_backoff_seconds(attempt))

        assert last is not None  # the loop runs at least once
        return last

    async def ping(self, *, timeout_seconds: float) -> CompletionResult:
        return await self.complete(
            role="spec",
            messages=[Message(role="user", content="ping")],
            timeout_seconds=timeout_seconds,
            max_output_tokens=1,
        )

    async def _attempt(
        self, payload: dict[str, Any], model_id: str, timeout_seconds: float, attempt: int
    ) -> CompletionResult:
        try:
            response = await self._client.chat.completions.create(
                **payload, timeout=timeout_seconds
            )
        except openai.APITimeoutError as exc:
            return CompletionFailure(
                reason="timeout",
                detail=str(exc),
                model_id=model_id,
                retryable=True,
                attempts=attempt,
            )
        except openai.RateLimitError as exc:
            return CompletionFailure(
                reason="rate_limited",
                detail=str(exc),
                model_id=model_id,
                retryable=True,
                attempts=attempt,
            )
        except openai.APIStatusError as exc:
            return CompletionFailure(
                reason="provider_error",
                detail=f"HTTP {exc.status_code}: {exc}",
                model_id=model_id,
                retryable=exc.status_code in _RETRYABLE_STATUS,
                attempts=attempt,
            )
        except openai.APIConnectionError as exc:
            return CompletionFailure(
                reason="provider_error",
                detail=f"connection: {exc}",
                model_id=model_id,
                retryable=True,
                attempts=attempt,
            )

        choice = response.choices[0]
        text = choice.message.content
        if text is None:
            # A completion with no content and no tool call is a refusal or a
            # provider bug; either way the caller has to decide, not us.
            return CompletionFailure(
                reason="refused",
                detail=f"empty content, finish_reason={choice.finish_reason}",
                model_id=model_id,
                retryable=False,
                attempts=attempt,
            )

        usage = response.usage
        return Completion(
            text=text,
            model_id=response.model,
            usage=Usage(
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
            ),
            finish_reason=choice.finish_reason or "unknown",
        )


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with full jitter, capped."""
    ceiling = min(30.0, 0.5 * (2 ** (attempt - 1)))
    return random.uniform(0.0, ceiling)  # noqa: S311 -- jitter, not cryptography
