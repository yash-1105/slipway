"""Role -> model routing, with a fallback and a price.

`get_model(role)` answers three questions from config/models.yaml: which model a
role uses, which model it falls back to, and what each costs per million tokens.
Nothing above this seam names a model; callers name a role, and
tests/unit/test_no_caller_names_a_model.py fails if that stops being true.

The router is itself a `ModelClient`, so everything above the seam is unchanged
by its existence. It wraps two underlying clients -- one whose catalogue maps
every role to its primary model, one to its fallback -- and tries the second
when the first fails in a way a different model might survive. Building it that
way means app/models/impl/novita.py, which is finished and tested, needed no
changes at all: it resolves a role through whichever catalogue it was given.

Every completion it returns is priced and recorded against the run in flight.
The cost is not written here -- it is drained by the worker and committed in the
same transaction as the job outcome, so a run cannot end up with an outcome and
no cost, or a cost and no outcome.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import structlog
import yaml

from app.domain.errors import ConfigError
from app.models.base import (
    Completion,
    CompletionFailure,
    CompletionResult,
    Message,
    ModelCatalogue,
    ModelClient,
    ModelSpec,
    Role,
)

log = structlog.get_logger(__name__)

#: The roles this system actually routes. Distinct from `Role` in base.py, which
#: carries one legacy value for a compatibility reason documented there.
RouterRole = Literal["planner", "builder", "evaluator", "test_author", "doc_writer"]

ROUTER_ROLES: tuple[RouterRole, ...] = (
    "planner",
    "builder",
    "evaluator",
    "test_author",
    "doc_writer",
)

#: The role the router's own health check uses.
PING_ROLE: RouterRole = "planner"

#: Failures a different model might survive. A refusal or an exhausted budget is
#: not one of them: the second model would refuse the same content, and falling
#: back on budget would spend past the ceiling that stopped us.
_FALLBACK_WORTHY = frozenset({"timeout", "rate_limited", "provider_error"})

_MILLION = Decimal(1_000_000)


# ---------------------------------------------------------------------------
# What a role resolves to
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """One model and its published price per million tokens."""

    model_id: str
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal

    def cost_usd(self, prompt_tokens: int, completion_tokens: int) -> Decimal:
        return (
            Decimal(prompt_tokens) / _MILLION * self.input_usd_per_mtok
            + Decimal(completion_tokens) / _MILLION * self.output_usd_per_mtok
        )


@dataclass(frozen=True, slots=True)
class RoleRouting:
    """Everything config/models.yaml says about one role."""

    role: str
    #: The model family a human intended for this role, in their words. Not a
    #: model id: the id comes from the provider's /models endpoint and is
    #: recorded in `primary`. Kept so a sync can flag when the id it matched no
    #: longer corresponds to what was asked for.
    intent: str
    context_window: int
    max_output_tokens: int
    primary: ModelPricing
    fallback: ModelPricing


@dataclass(frozen=True, slots=True)
class RoutingTable:
    """config/models.yaml, parsed."""

    source_base_url: str
    synced_at: str
    by_role: dict[str, RoleRouting] = field(default_factory=dict)
    available: tuple[str, ...] = ()

    def get_model(self, role: str) -> RoleRouting:
        """The routing for `role`.

        Raises rather than returning None: a role with no model is a
        configuration error, and discovering it at the first agent call is
        exactly the failure mode config/models.yaml exists to prevent.
        """
        routing = self.by_role.get(role)
        if routing is None:
            known = ", ".join(sorted(self.by_role)) or "none"
            raise ConfigError(
                f"no model configured for role {role!r} in config/models.yaml "
                f"(configured roles: {known}; catalogue synced {self.synced_at}). "
                "Run `make models-sync` and assign the role."
            )
        return routing


def get_model(routing: RoutingTable, role: str) -> RoleRouting:
    """Module-level form of `RoutingTable.get_model`."""
    return routing.get_model(role)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _pricing(where: str, raw: object) -> ModelPricing:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping with model_id and prices")

    missing = [
        key
        for key in ("model_id", "input_usd_per_mtok", "output_usd_per_mtok")
        if key not in raw
    ]
    if missing:
        raise ConfigError(f"{where} is missing {', '.join(missing)}")

    model_id = str(raw["model_id"]).strip()
    if not model_id:
        raise ConfigError(f"{where}.model_id is empty")

    try:
        input_price = Decimal(str(raw["input_usd_per_mtok"]))
        output_price = Decimal(str(raw["output_usd_per_mtok"]))
    except ArithmeticError as exc:
        raise ConfigError(f"{where}: prices must be numbers") from exc

    if input_price < 0 or output_price < 0:
        raise ConfigError(f"{where}: prices cannot be negative")

    return ModelPricing(
        model_id=model_id,
        input_usd_per_mtok=input_price,
        output_usd_per_mtok=output_price,
    )


def load_routing(path: Path, *, require_models: bool) -> RoutingTable:
    """Read config/models.yaml.

    `require_models` is False when the models seam is the fake one, so a
    checkout with no Novita key still boots for tests and frontend work.
    """
    if not path.is_file():
        if not require_models:
            return RoutingTable(source_base_url="", synced_at="never")
        raise ConfigError(
            f"{path} does not exist. Run `make models-sync` with a live "
            "SLIPWAY_NOVITA_API_KEY to generate it from the provider's /models "
            "endpoint. Do not write it by hand."
        )

    raw: Any = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} is not a YAML mapping")

    roles_raw = raw.get("roles") or {}
    if not isinstance(roles_raw, dict):
        raise ConfigError(f"{path}: `roles` must be a mapping of role -> routing")

    by_role: dict[str, RoleRouting] = {}
    for role, entry in roles_raw.items():
        where = f"{path}: roles.{role}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be a mapping")

        for key in ("primary", "fallback", "context_window", "max_output_tokens"):
            if key not in entry:
                raise ConfigError(f"{where} is missing {key!r}")

        by_role[str(role)] = RoleRouting(
            role=str(role),
            intent=str(entry.get("intent", "")),
            context_window=int(entry["context_window"]),
            max_output_tokens=int(entry["max_output_tokens"]),
            primary=_pricing(f"{where}.primary", entry["primary"]),
            fallback=_pricing(f"{where}.fallback", entry["fallback"]),
        )

    if require_models:
        unconfigured = [role for role in ROUTER_ROLES if role not in by_role]
        if unconfigured:
            raise ConfigError(
                f"{path} does not configure {', '.join(unconfigured)}. "
                "Every role needs a primary, a fallback and both prices before "
                "the models seam will start. Run `make models-sync`. See "
                "docs/decisions/0004-novita-base-url.md."
            )

    available = raw.get("available") or []
    return RoutingTable(
        source_base_url=str(raw.get("source_base_url", "")),
        synced_at=str(raw.get("synced_at", "unknown")),
        by_role=by_role,
        available=tuple(str(model_id) for model_id in available),
    )


def catalogue_for(routing: RoutingTable, which: Literal["primary", "fallback"]) -> ModelCatalogue:
    """A flat role -> model view, for a client that resolves one model per role.

    The router builds two of these -- one primary, one fallback -- and hands
    each to its own client. That is what lets the underlying client stay
    completely unaware that a fallback exists.
    """
    return ModelCatalogue(
        source_base_url=routing.source_base_url,
        synced_at=routing.synced_at,
        by_role={
            role: ModelSpec(
                role=role,
                model_id=(entry.primary if which == "primary" else entry.fallback).model_id,
                context_window=entry.context_window,
                max_output_tokens=entry.max_output_tokens,
                notes=entry.intent,
            )
            for role, entry in routing.by_role.items()
        },
    )


# ---------------------------------------------------------------------------
# Who a call is for
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CallContext:
    """The run, job and person a model call is being made on behalf of."""

    run_id: UUID
    actor: str
    job_id: UUID | None = None


_CALL_CONTEXT: contextvars.ContextVar[CallContext | None] = contextvars.ContextVar(
    "slipway_model_call_context", default=None
)


@contextlib.contextmanager
def acting_as(context: CallContext) -> Iterator[None]:
    """Attribute every model call made inside this block to `context`.

    A context variable rather than a parameter, because the alternative is
    threading run_id and actor through `ModelClient.complete`, every agent graph
    and every node -- which would mean changing the seam's Protocol and every
    implementation of it, including one that is finished and tested.
    """
    token = _CALL_CONTEXT.set(context)
    try:
        yield
    finally:
        _CALL_CONTEXT.reset(token)


def current_context() -> CallContext | None:
    return _CALL_CONTEXT.get()


# ---------------------------------------------------------------------------
# What a call cost
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CostRecord:
    """One priced model call, before it is attributed to a run and written."""

    role: str
    #: What we asked the provider for.
    model_requested: str
    #: What the provider says it actually served. Not always the same thing, and
    #: the ledger records what happened rather than what was intended.
    model_used: str
    prompt_tokens: int
    completion_tokens: int
    usd: Decimal
    inr: Decimal
    usd_to_inr: Decimal
    actor: str
    run_id: UUID
    job_id: UUID | None
    #: False when the provider served a model we hold no published price for, so
    #: the amounts are the requested model's price applied to someone else's
    #: tokens. A zero that means "free" and a zero that means "unknown" are
    #: different numbers and the ledger has to be able to tell them apart.
    priced: bool = True


class CostCollector:
    """Holds priced calls until the worker commits them with the job outcome.

    Not a repository: it writes nothing. The worker drains it inside the same
    transaction that records what the job produced, which is what stops cost and
    outcome from diverging.
    """

    def __init__(self) -> None:
        self._records: list[CostRecord] = []

    def record(self, record: CostRecord) -> None:
        self._records.append(record)

    def drain(self, run_id: UUID) -> list[CostRecord]:
        """Take everything recorded for one run, leaving other runs alone."""
        taken = [r for r in self._records if r.run_id == run_id]
        self._records = [r for r in self._records if r.run_id != run_id]
        return taken

    def pending(self) -> int:
        return len(self._records)


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------


class ModelRouter(ModelClient):
    def __init__(
        self,
        *,
        routing: RoutingTable,
        primary: ModelClient,
        fallback: ModelClient,
        collector: CostCollector,
        usd_to_inr: Decimal,
    ) -> None:
        self._routing = routing
        self._primary = primary
        self._fallback = fallback
        self._collector = collector
        self._usd_to_inr = usd_to_inr

    def get_model(self, role: str) -> RoleRouting:
        return self._routing.get_model(role)

    async def complete(
        self,
        *,
        role: Role,
        messages: list[Message],
        timeout_seconds: float,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> CompletionResult:
        routing = self._routing.get_model(role)

        result = await self._primary.complete(
            role=role,
            messages=messages,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )

        if isinstance(result, Completion):
            self._price(role, routing, routing.primary, result)
            return result

        if not _worth_falling_back(result):
            log.info(
                "models.no_fallback",
                role=role,
                reason=result.reason,
                model=routing.primary.model_id,
                detail="a different model would fail the same way",
            )
            return result

        log.warning(
            "models.fell_back",
            role=role,
            primary=routing.primary.model_id,
            fallback=routing.fallback.model_id,
            reason=result.reason,
            attempts=result.attempts,
            detail=result.detail[:200],
        )

        second = await self._fallback.complete(
            role=role,
            messages=messages,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )

        if isinstance(second, Completion):
            self._price(role, routing, routing.fallback, second)
            return second

        log.error(
            "models.both_failed",
            role=role,
            primary=routing.primary.model_id,
            fallback=routing.fallback.model_id,
            primary_reason=result.reason,
            fallback_reason=second.reason,
        )
        return second

    async def ping(self, *, timeout_seconds: float) -> CompletionResult:
        return await self.complete(
            role=PING_ROLE,
            messages=[Message(role="user", content="ping")],
            timeout_seconds=timeout_seconds,
            max_output_tokens=1,
        )

    def _price(
        self,
        role: str,
        routing: RoleRouting,
        attempted: ModelPricing,
        completion: Completion,
    ) -> None:
        context = current_context()
        if context is None:
            # Nothing to attribute it to. A model call outside a run is a bug or
            # a `slipway models ping`; either way an unattributed ledger row is
            # worse than none.
            log.debug("models.unattributed_call", role=role, model=completion.model_id)
            return

        pricing, priced = self._pricing_for(routing, attempted, completion.model_id)
        usd = pricing.cost_usd(
            completion.usage.prompt_tokens, completion.usage.completion_tokens
        )

        self._collector.record(
            CostRecord(
                role=role,
                model_requested=attempted.model_id,
                model_used=completion.model_id,
                prompt_tokens=completion.usage.prompt_tokens,
                completion_tokens=completion.usage.completion_tokens,
                usd=usd,
                inr=usd * self._usd_to_inr,
                usd_to_inr=self._usd_to_inr,
                actor=context.actor,
                run_id=context.run_id,
                job_id=context.job_id,
                priced=priced,
            )
        )

    def _pricing_for(
        self, routing: RoleRouting, attempted: ModelPricing, served: str
    ) -> tuple[ModelPricing, bool]:
        """Price against the model the provider says it served.

        Falls back to the price of what we asked for, flagged unpriced, when the
        provider serves something else. Recording the requested model's price
        for someone else's tokens is a guess, and the flag is what stops it
        being read as a measurement.
        """
        for candidate in (attempted, routing.primary, routing.fallback):
            if candidate.model_id == served:
                return candidate, True

        log.warning(
            "models.unpriced_model",
            requested=attempted.model_id,
            served=served,
            detail="provider served a model with no published price in config/models.yaml",
        )
        return attempted, False


def _worth_falling_back(failure: CompletionFailure) -> bool:
    return failure.reason in _FALLBACK_WORTHY
