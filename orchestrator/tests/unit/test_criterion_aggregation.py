"""Rolling test results up to criteria.

This is the shape the evaluator reads, so the cases that matter are the ones a
list of test results cannot express: a criterion nobody tested, and a criterion
whose tests disagree.
"""

from __future__ import annotations

import pytest

from app.services.testing import (
    TestOutcome,
    aggregate_by_criterion,
    criteria_in,
)


def outcome(title: str, status: str = "passed", criteria: tuple[str, ...] = ()) -> TestOutcome:
    return TestOutcome(
        title=title, file="spec.ts", line=1, status=status,  # type: ignore[arg-type]
        duration_ms=10, criteria=criteria or criteria_in(title),
    )


# --- declaring a criterion -------------------------------------------------


def test_a_criterion_is_read_from_the_title() -> None:
    assert criteria_in("[AC-1] the form submits") == ("AC-1",)


def test_a_test_may_declare_several() -> None:
    assert criteria_in("[AC-1, AC-2] the form submits and records") == ("AC-1", "AC-2")


def test_a_repeated_criterion_is_listed_once() -> None:
    assert criteria_in("[AC-3] AC-3 again") == ("AC-3",)


def test_annotations_are_read_too() -> None:
    found = criteria_in(
        "the form submits",
        [{"type": "criterion", "description": "AC-7"}],
    )
    assert found == ("AC-7",)


def test_a_title_with_no_criterion_declares_none() -> None:
    """Not an error. It is a test that verifies no numbered criterion."""
    assert criteria_in("a smoke test") == ()


def test_a_near_miss_is_not_a_criterion() -> None:
    """A typo must read as no criterion, not as one nobody declared."""
    assert criteria_in("[AC 1] spaced") == ()
    assert criteria_in("[ACC-1] wrong prefix") == ()


# --- aggregation -----------------------------------------------------------


def test_a_criterion_whose_tests_all_pass_is_verified() -> None:
    results = aggregate_by_criterion((outcome("[AC-1] a"), outcome("[AC-1] b")))
    assert len(results) == 1
    assert results[0].status == "verified"
    assert results[0].tests_passed == 2


def test_one_failing_test_fails_the_criterion() -> None:
    """Not a majority vote. A criterion with a failing test is not verified."""
    results = aggregate_by_criterion(
        (outcome("[AC-1] a"), outcome("[AC-1] b", status="failed"))
    )
    assert results[0].status == "failed"
    assert results[0].tests_passed == 1
    assert results[0].tests_failed == 1
    assert results[0].failing_tests == ("[AC-1] b",)


def test_a_criterion_with_no_test_is_untested_not_passed() -> None:
    """The case a list of test results cannot express, and the one that matters.

    An all-green suite that never touched AC-2 must not report AC-2 as
    satisfied. Silence is not evidence.
    """
    results = aggregate_by_criterion(
        (outcome("[AC-1] a"),), expected=("AC-1", "AC-2")
    )
    by_id = {c.criterion: c for c in results}

    assert by_id["AC-1"].status == "verified"
    assert by_id["AC-2"].status == "untested"
    assert by_id["AC-2"].tests_passed == 0
    assert by_id["AC-2"].tests_failed == 0


def test_a_criterion_a_test_declares_but_the_spec_did_not_still_appears() -> None:
    """A test verifying something nobody asked for is worth seeing, not hiding."""
    results = aggregate_by_criterion((outcome("[AC-9] extra"),), expected=("AC-1",))
    ids = [c.criterion for c in results]
    assert ids == ["AC-1", "AC-9"]


def test_expected_criteria_keep_their_declared_order() -> None:
    """The report is read next to the spec; matching its order costs nothing."""
    results = aggregate_by_criterion((), expected=("AC-3", "AC-1", "AC-2"))
    assert [c.criterion for c in results] == ["AC-3", "AC-1", "AC-2"]


def test_a_skipped_test_does_not_fail_its_criterion() -> None:
    results = aggregate_by_criterion((outcome("[AC-1] a", status="skipped"),))
    assert results[0].status == "verified"


def test_a_timed_out_test_fails_its_criterion() -> None:
    results = aggregate_by_criterion((outcome("[AC-1] a", status="timedOut"),))
    assert results[0].status == "failed"


@pytest.mark.parametrize("status", ["failed", "timedOut", "interrupted"])
def test_every_non_passing_status_fails_the_criterion(status: str) -> None:
    results = aggregate_by_criterion((outcome("[AC-1] a", status=status),))
    assert results[0].status == "failed"


def test_one_test_covering_two_criteria_counts_for_both() -> None:
    results = aggregate_by_criterion((outcome("[AC-1, AC-2] both", status="failed"),))
    assert {c.criterion: c.status for c in results} == {"AC-1": "failed", "AC-2": "failed"}
