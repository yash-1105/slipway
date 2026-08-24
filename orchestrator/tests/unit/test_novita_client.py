"""The Novita client's retry and failure-classification behaviour.

The OpenAI SDK is mocked at the seam -- it is the collaborator, not the thing
under test. Assertions are on the CompletionResult that comes back.
"""

from __future__ import annotations

from typing import Any

# The OpenAI SDK vendors httpx2, not httpx. Its exception constructors are typed
# against that module, so building one for a test has to use the same one.
import httpx2
import openai
import pytest

from app.models.base import Completion, CompletionFailure, Message, ModelCatalogue, ModelSpec
from app.models.impl.novita import NovitaModelClient

CATALOGUE = ModelCatalogue(
    source_base_url="https://api.novita.ai/openai/v1",
    synced_at="2026-08-24T00:00:00Z",
    by_role={
        "planner": ModelSpec(
            role="planner",
            model_id="a-model-id-read-from-the-catalogue",
            context_window=128_000,
            max_output_tokens=4096,
        )
    },
)


class _StubCompletions:
    """Stands in for client.chat.completions."""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        outcome = self._outcomes.pop(0) if self._outcomes else self._outcomes
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _response(content: str | None, *, finish_reason: str = "stop") -> Any:
    class _Message:
        def __init__(self) -> None:
            self.content = content

    class _Choice:
        def __init__(self) -> None:
            self.message = _Message()
            self.finish_reason = finish_reason

    class _Usage:
        prompt_tokens = 10
        completion_tokens = 5

    class _Response:
        def __init__(self) -> None:
            self.choices = [_Choice()]
            self.usage = _Usage()
            self.model = "a-model-id-read-from-the-catalogue"

    return _Response()


def _client(
    outcomes: list[Any], *, max_attempts: int = 3
) -> tuple[NovitaModelClient, _StubCompletions]:
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    client = NovitaModelClient(
        api_key="test-key",
        base_url="https://api.novita.ai/openai/v1",
        catalogue=CATALOGUE,
        default_timeout_seconds=5.0,
        max_attempts=max_attempts,
        sleep=record,
    )
    client.slept = slept  # type: ignore[attr-defined]
    stub = _StubCompletions(outcomes)

    class _Chat:
        completions = stub

    # `chat` is a read-only property on the SDK client; overriding it is how
    # the transport is replaced without touching the code under test.
    object.__setattr__(client._client, "chat", _Chat())
    return client, stub


def _status_error(status: int) -> openai.APIStatusError:
    request = httpx2.Request("POST", "https://api.novita.ai/openai/v1/chat/completions")
    response = httpx2.Response(status, request=request, json={"error": "nope"})
    return openai.APIStatusError("boom", response=response, body=None)


MESSAGES = [Message(role="user", content="hello")]


async def test_a_successful_call_returns_the_text_and_usage() -> None:
    client, stub = _client([_response("the specification")])

    result = await client.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)

    assert isinstance(result, Completion)
    assert result.text == "the specification"
    assert result.usage.total_tokens == 15
    assert stub.calls == 1


async def test_a_timeout_is_retried_and_then_returned_as_a_value() -> None:
    """A failure the caller must act on is a value, not an exception."""
    client, stub = _client(
        [openai.APITimeoutError(request=httpx2.Request("POST", "https://x")) for _ in range(3)]
    )

    result = await client.complete(role="planner", messages=MESSAGES, timeout_seconds=0.01)

    assert isinstance(result, CompletionFailure)
    assert result.reason == "timeout"
    assert result.attempts == 3
    assert stub.calls == 3


async def test_a_transient_failure_followed_by_success_returns_the_success() -> None:
    client, stub = _client([_status_error(503), _response("recovered")])

    result = await client.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)

    assert isinstance(result, Completion)
    assert result.text == "recovered"
    assert stub.calls == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_a_permanent_failure_is_not_retried(status: int) -> None:
    """Retrying a 401 just spends the rate limit on the same wrong answer."""
    client, stub = _client([_status_error(status) for _ in range(3)])

    result = await client.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)

    assert isinstance(result, CompletionFailure)
    assert result.retryable is False
    assert stub.calls == 1


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
async def test_a_transient_status_is_retried_up_to_the_cap(status: int) -> None:
    client, stub = _client([_status_error(status) for _ in range(5)], max_attempts=3)

    result = await client.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)

    assert isinstance(result, CompletionFailure)
    assert stub.calls == 3, "retries must be capped, not unbounded"


async def test_an_empty_completion_is_reported_as_a_refusal() -> None:
    """Content of None is a refusal or a provider bug; the caller decides."""
    client, _ = _client([_response(None, finish_reason="content_filter")])

    result = await client.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)

    assert isinstance(result, CompletionFailure)
    assert result.reason == "refused"
    assert "content_filter" in result.detail


async def test_a_role_missing_from_the_catalogue_fails_before_any_call() -> None:
    """No model id is ever typed from memory; an unconfigured role must not guess."""
    client, stub = _client([_response("should not happen")])

    result = await client.complete(role="builder", messages=MESSAGES, timeout_seconds=5.0)

    assert isinstance(result, CompletionFailure)
    assert "models.yaml" in result.detail
    assert stub.calls == 0


async def test_the_model_id_comes_from_the_catalogue_not_from_code() -> None:
    client, stub = _client([_response("ok")])
    seen: dict[str, Any] = {}

    async def capture(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return _response("ok")

    stub.create = capture  # type: ignore[method-assign]

    await client.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)

    assert seen["model"] == "a-model-id-read-from-the-catalogue"
    assert seen["timeout"] == 5.0, "every external call carries an explicit timeout"


async def test_backoff_grows_and_stays_bounded() -> None:
    """Retries back off, and the wait is capped rather than doubling forever."""
    client, _ = _client([_status_error(503) for _ in range(10)], max_attempts=8)

    await client.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)

    slept: list[float] = client.slept  # type: ignore[attr-defined]
    assert len(slept) == 7, "one sleep between each of 8 attempts"
    assert all(0.0 <= seconds <= 30.0 for seconds in slept), "the cap is 30 seconds"
    assert max(slept) < 31.0


async def test_a_successful_call_never_sleeps() -> None:
    client, _ = _client([_response("first try")])

    await client.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)

    assert client.slept == []  # type: ignore[attr-defined]
