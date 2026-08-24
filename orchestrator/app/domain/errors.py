"""Domain errors.

Only for things a caller cannot sensibly act on in-band: a violated invariant,
a missing row, a bad transition. Failures the caller must *act* on -- a build
that did not compile, a deploy that did not come up -- are values, not
exceptions; see the seam result types in app/*/base.py.
"""

from __future__ import annotations


class SlipwayError(Exception):
    """Base for every error Slipway raises deliberately."""


class NotFoundError(SlipwayError):
    """A row that the caller referenced by id does not exist."""

    def __init__(self, entity: str, key: object) -> None:
        super().__init__(f"{entity} not found: {key}")
        self.entity = entity
        self.key = key


class IllegalTransitionError(SlipwayError):
    """A run was asked to move somewhere the transition table forbids."""

    def __init__(self, state: str, trigger: str) -> None:
        super().__init__(f"no transition from {state!r} on {trigger!r}")
        self.state = state
        self.trigger = trigger


class AlreadyDecidedError(SlipwayError):
    """A gate that already has a decision was decided again."""

    def __init__(self, run_id: object, gate: str) -> None:
        super().__init__(f"run {run_id} already has a decision for gate {gate!r}")
        self.run_id = run_id
        self.gate = gate


class ResourceExhaustedError(SlipwayError):
    """A pool with a hard bound -- ports, budget -- has nothing left."""


class ConfigError(SlipwayError):
    """Configuration is missing or inconsistent. Raised only at startup."""
