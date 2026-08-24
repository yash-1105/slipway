"""Role resolution, fallback, and what each call is priced at."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from app.domain.errors import ConfigError
from app.domain.ids import uuid7
from app.models.base import (
    Completion,
    CompletionFailure,
    CompletionResult,
    Message,
    ModelCatalogue,
    Role,
    Usage,
)
from app.models.router import (
    CallContext,
    CostCollector,
    ModelPricing,
    ModelRouter,
    RoleRouting,
    RoutingTable,
    acting_as,
    catalogue_for,
    get_model,
)

RUN_ID = uuid7()
MESSAGES = [Message(role="user", content="hello")]

PRIMARY = ModelPricing(
    model_id="vendor-a/primary-model",
    input_usd_per_mtok=Decimal("2"),
    output_usd_per_mtok=Decimal("10"),
)
FALLBACK = ModelPricing(
    model_id="vendor-b/fallback-model",
    input_usd_per_mtok=Decimal("1"),
    output_usd_per_mtok=Decimal("4"),
)

ROUTING = RoutingTable(
    source_base_url="https://example.invalid/v1",
    synced_at="2026-08-24T00:00:00Z",
    by_role={
        "planner": RoleRouting(
            role="planner",
            intent="an intended model family",
            context_window=128_000,
            max_output_tokens=8_192,
            primary=PRIMARY,
            fallback=FALLBACK,
        )
    },
)


class _Stub:
    """Returns queued outcomes. A real implementation of the Protocol."""

    def __init__(self, outcomes: list[CompletionResult]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def complete(
        self,
        *,
        role: Role,
        messages: list[Message],
        timeout_seconds: float,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> CompletionResult:
        self.calls += 1
        return self._outcomes.pop(0)

    async def ping(self, *, timeout_seconds: float) -> CompletionResult:
        raise AssertionError("the router pings through complete()")


def completion(model_id: str, *, prompt: int = 1000, output: int = 500) -> Completion:
    return Completion(
        text="ok",
        model_id=model_id,
        usage=Usage(prompt_tokens=prompt, completion_tokens=output),
        finish_reason="stop",
    )


def failure(reason: str) -> CompletionFailure:
    return CompletionFailure(reason=reason, detail="boom", retryable=True, attempts=3)  # type: ignore[arg-type]


def router(
    primary: list[CompletionResult], fallback: list[CompletionResult]
) -> tuple[ModelRouter, _Stub, _Stub, CostCollector]:
    first, second = _Stub(primary), _Stub(fallback)
    collector = CostCollector()
    return (
        ModelRouter(
            routing=ROUTING,
            primary=first,
            fallback=second,
            collector=collector,
            usd_to_inr=Decimal("80"),
        ),
        first,
        second,
        collector,
    )


# --- get_model -------------------------------------------------------------


def test_get_model_returns_primary_fallback_and_prices() -> None:
    routing = get_model(ROUTING, "planner")

    assert routing.primary.model_id == "vendor-a/primary-model"
    assert routing.fallback.model_id == "vendor-b/fallback-model"
    assert routing.primary.input_usd_per_mtok == Decimal("2")
    assert routing.fallback.output_usd_per_mtok == Decimal("4")


def test_an_unconfigured_role_is_a_configuration_error_naming_the_role() -> None:
    with pytest.raises(ConfigError, match="doc_writer"):
        get_model(ROUTING, "doc_writer")


def test_the_two_catalogues_differ_only_in_which_model_they_name() -> None:
    """This is what lets the underlying client stay unaware a fallback exists."""
    primary = catalogue_for(ROUTING, "primary")
    fallback = catalogue_for(ROUTING, "fallback")

    assert isinstance(primary, ModelCatalogue)
    assert primary.by_role["planner"].model_id == "vendor-a/primary-model"
    assert fallback.by_role["planner"].model_id == "vendor-b/fallback-model"
    assert (
        primary.by_role["planner"].context_window == fallback.by_role["planner"].context_window
    )


# --- fallback --------------------------------------------------------------


async def test_a_working_primary_is_not_second_guessed() -> None:
    subject, first, second, _ = router([completion(PRIMARY.model_id)], [])

    result = await subject.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)

    assert isinstance(result, Completion)
    assert first.calls == 1
    assert second.calls == 0


@pytest.mark.parametrize("reason", ["timeout", "rate_limited", "provider_error"])
async def test_a_failure_a_different_model_might_survive_falls_back(reason: str) -> None:
    subject, first, second, _ = router([failure(reason)], [completion(FALLBACK.model_id)])

    result = await subject.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)

    assert isinstance(result, Completion)
    assert result.model_id == FALLBACK.model_id
    assert first.calls == 1
    assert second.calls == 1


@pytest.mark.parametrize("reason", ["refused", "budget_exhausted"])
async def test_a_failure_a_different_model_would_repeat_does_not_fall_back(reason: str) -> None:
    """A refusal is about the content; a budget stop is about the ceiling.

    Retrying either on a second model spends money to reach the same answer.
    """
    subject, _, second, _ = router([failure(reason)], [completion(FALLBACK.model_id)])

    result = await subject.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)

    assert isinstance(result, CompletionFailure)
    assert second.calls == 0


async def test_both_failing_returns_the_fallback_failure() -> None:
    subject, _, _, _ = router([failure("timeout")], [failure("provider_error")])

    result = await subject.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)

    assert isinstance(result, CompletionFailure)
    assert result.reason == "provider_error"


# --- pricing ---------------------------------------------------------------


async def test_a_completion_is_priced_against_the_model_that_served_it() -> None:
    subject, _, _, collector = router([completion(PRIMARY.model_id)], [])

    with acting_as(CallContext(run_id=RUN_ID, actor="worker:test", job_id=None)):
        await subject.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)

    (record,) = collector.drain(RUN_ID)
    # 1000 prompt tokens at $2/Mtok + 500 output at $10/Mtok.
    assert record.usd == Decimal("0.002") + Decimal("0.005")
    assert record.inr == record.usd * Decimal("80")
    assert record.usd_to_inr == Decimal("80")
    assert record.priced is True


async def test_a_fallback_is_priced_at_the_fallback_model_not_the_primary() -> None:
    """The ledger records what was served, not what was intended."""
    subject, _, _, collector = router([failure("timeout")], [completion(FALLBACK.model_id)])

    with acting_as(CallContext(run_id=RUN_ID, actor="worker:test", job_id=None)):
        await subject.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)

    (record,) = collector.drain(RUN_ID)
    assert record.model_requested == FALLBACK.model_id
    assert record.model_used == FALLBACK.model_id
    assert record.usd == Decimal("0.001") + Decimal("0.002")


async def test_a_substituted_model_is_recorded_as_unpriced() -> None:
    """A zero meaning free and a zero meaning unknown are different numbers."""
    subject, _, _, collector = router([completion("vendor-c/something-else-3")], [])

    with acting_as(CallContext(run_id=RUN_ID, actor="worker:test", job_id=None)):
        await subject.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)

    (record,) = collector.drain(RUN_ID)
    assert record.model_used == "vendor-c/something-else-3"
    assert record.model_requested == PRIMARY.model_id
    assert record.priced is False


async def test_the_acting_user_is_recorded() -> None:
    subject, _, _, collector = router([completion(PRIMARY.model_id)], [])

    with acting_as(CallContext(run_id=RUN_ID, actor="yash", job_id=None)):
        await subject.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)

    (record,) = collector.drain(RUN_ID)
    assert record.actor == "yash"
    assert record.run_id == RUN_ID


async def test_a_call_with_no_context_records_nothing() -> None:
    """An unattributed ledger row is worse than none: it cannot be reconciled."""
    subject, _, _, collector = router([completion(PRIMARY.model_id)], [])

    await subject.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)

    assert collector.pending() == 0


async def test_draining_one_run_leaves_another_runs_costs_alone() -> None:
    other = uuid7()
    subject, _, _, collector = router(
        [completion(PRIMARY.model_id), completion(PRIMARY.model_id)], []
    )

    with acting_as(CallContext(run_id=RUN_ID, actor="w", job_id=None)):
        await subject.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)
    with acting_as(CallContext(run_id=other, actor="w", job_id=None)):
        await subject.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)

    assert len(collector.drain(RUN_ID)) == 1
    assert collector.pending() == 1
    assert len(collector.drain(other)) == 1


async def test_a_failed_call_costs_nothing_because_nothing_was_returned() -> None:
    """No usage means no row. The failure is recorded as an event, not a cost."""
    subject, _, _, collector = router([failure("refused")], [])

    with acting_as(CallContext(run_id=RUN_ID, actor="w", job_id=None)):
        await subject.complete(role="planner", messages=MESSAGES, timeout_seconds=5.0)

    assert collector.pending() == 0


def test_pricing_is_decimal_not_float() -> None:
    """Money in binary floating point is a reconciliation nobody can close."""
    assert isinstance(PRIMARY.cost_usd(1_000_000, 0), Decimal)
    assert PRIMARY.cost_usd(1_000_000, 0) == Decimal("2")
    assert PRIMARY.cost_usd(0, 1_000_000) == Decimal("10")
    assert PRIMARY.cost_usd(0, 0) == Decimal("0")


def test_a_context_is_restored_after_the_block() -> None:
    assert _ctx() is None
    with acting_as(CallContext(run_id=RUN_ID, actor="a", job_id=None)):
        assert _ctx() is not None
    assert _ctx() is None


def _ctx() -> CallContext | None:
    from app.models.router import current_context

    return current_context()


def test_uuid_type_is_preserved() -> None:
    assert isinstance(RUN_ID, UUID)
