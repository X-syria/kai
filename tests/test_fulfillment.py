"""Contract-bound fulfillment evaluation — no database, no HTTP, no model.

The authority chain under test:

```text
Sealed SuccessCriteria  ->  calendar evaluator  ->  FulfillmentVerdict
                                                     ->  Task State Machine
```

These tests pin the binding itself: that the sealed criteria are the authority
and the prose is not, that the profile in the contract is what selects an
evaluator, that the verdict is a pure function of durable inputs, and that an
evaluator is structurally incapable of transitioning a task.

The semantic-preservation tests are the point of the whole exercise: every
verdict asserted in the evidence-set section below is the verdict the
prose-era implementation gave for the same window, identities, and log.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import date, time
from types import SimpleNamespace
from typing import Any, Final

import pytest
from pydantic import ValidationError

from kai.runtime.fulfillment import (
    CALENDAR_MEETING_EVALUATOR_VERSION,
    CalendarMeetingCriteria,
    FulfillmentInput,
    FulfillmentOutcome,
    FulfillmentVerdict,
    SuccessCriteria,
    _meeting_satisfies_criteria,
    calendar_meeting_criteria,
    evaluate_calendar_meeting,
)
from kai.runtime.schemas import EXTERNAL_OUTCOME_UNKNOWN, MeetingWindow
from kai.runtime.service import RuntimeFatal, _fulfillment_outcome
from kai.tasks.schemas import TaskContractInput
from kai.tools.errors import (
    APPROVAL_STALE,
    PROVIDER_ERROR,
    PROVIDER_NOT_CONTACTED,
    PROVIDER_SERVER_ERROR,
    PROVIDER_UNREACHABLE,
)

WINDOW: Final = MeetingWindow(
    start_date=date(2026, 6, 22),
    end_date=date(2026, 6, 26),
    earliest_time=time(16, 0),
    title="KAI meeting",
)

IDENTITIES: Final = ({"email": "Ahmed@Example.com", "name": "Ahmed"},)


def _criteria(
    *, window: MeetingWindow = WINDOW, identities: Any = IDENTITIES
) -> SuccessCriteria:
    """The sealed success criteria for one window and one resolved guest set."""
    return calendar_meeting_criteria(
        profile="calendar_meeting", window=window, identities=identities
    )


def _evaluator_source(function: Callable[..., Any]) -> str:
    """The implementation of one fulfillment function, as text.

    Used to assert what the evaluator does *not* reference. A behavioural test can
    show that the criteria change the verdict, but only reading the source can show
    that prose is nowhere in the decision — which is the claim being made.
    """
    return inspect.getsource(function)


def _code_of(function: Callable[..., Any]) -> str:
    """The same source with the docstring removed.

    Needed where a *word* is the thing under test: a docstring may discuss the rule
    it is asserting, and that discussion is not the rule.
    """
    tree = ast.parse(inspect.getsource(function))
    node = tree.body[0]
    if (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    ):
        node.body.pop(0)
    return ast.unparse(node)


def _created(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "tool": "calendar.create_event",
        "outcome": "EXECUTION_SUCCEEDED",
        "ok": True,
        "verified": True,
        "args": {
            "date": "2026-06-24",
            "start_time": "17:30",
            "attendees": ["ahmed@example.com"],
        },
        "evidence": {"event_id": "evt-1"},
    }
    entry.update(overrides)
    return entry


def _inputs(*evidence: dict[str, Any], identities: Any = IDENTITIES) -> FulfillmentInput:
    return FulfillmentInput(
        criteria=_criteria(identities=identities),
        evidence=tuple(evidence),
    )


# --- helpers for describing the relevant evidence set -------------------------


def _verdict(*evidence: dict[str, Any], identities: Any = IDENTITIES) -> FulfillmentVerdict:
    """The verdict for one ordering of one relevant-evidence set."""
    return evaluate_calendar_meeting(_inputs(*evidence, identities=identities))


def _permutations(items: list[dict[str, Any]]) -> list[tuple[dict[str, Any], ...]]:
    """Every ordering of ``items``, so invariance is checked, not assumed."""
    if len(items) <= 1:
        return [tuple(items)]
    out: list[tuple[dict[str, Any], ...]] = []
    for index, item in enumerate(items):
        rest = items[:index] + items[index + 1 :]
        for tail in _permutations(rest):
            out.append((item, *tail))
    return out


def _misplaced_creation(**overrides: Any) -> dict[str, Any]:
    """A qualifying creation that satisfies no criterion.

    Qualifying, because the provider confirmed it — but it booked the wrong day,
    which is exactly the kind of record that must not decide the answer.
    """
    values: dict[str, Any] = {
        "args": {
            "date": "2026-06-30",
            "start_time": "17:30",
            "attendees": ["ahmed@example.com"],
        },
        "evidence": {"event_id": "evt-wrong"},
    }
    values.update(overrides)
    return _created(**values)


def _lost_answer(**overrides: Any) -> dict[str, Any]:
    """A creation attempt whose external outcome KAI could not establish.

    Deliberately shaped like a *refused* run in every respect except the one that
    matters: ``ok`` is false and there is no provider event id, so it cannot
    become a witness by accident.

    **The field that makes it unknown is ``external_outcome``, not
    ``error_code``.** The runtime writes that key as its own conclusion about the
    provider's side; ``error_code`` reports why the gateway returned what it
    returned, which is a different question. So the default here pairs a
    known refusal code with an unestablished world — the exact combination that
    was unrepresentable before, and the reason the two were being conflated.
    """
    entry: dict[str, Any] = {
        "tool": "calendar.create_event",
        "outcome": "EXECUTION_FAILED",
        "ok": False,
        "verified": False,
        "error_code": APPROVAL_STALE,
        "external_outcome": EXTERNAL_OUTCOME_UNKNOWN,
        "args": {
            "date": "2026-06-24",
            "start_time": "17:30",
            "attendees": ["ahmed@example.com"],
        },
        "evidence": {},
    }
    entry.update(overrides)
    return entry


# --- an unestablished outcome is its own answer, not a failure --------------------


@pytest.mark.parametrize(
    "error_code",
    [APPROVAL_STALE, PROVIDER_UNREACHABLE, PROVIDER_SERVER_ERROR],
)
def test_an_unestablished_external_outcome_is_indeterminate_not_not_fulfilled(
    error_code: str,
) -> None:
    """The empty witness set does not prove absence when the outcome is unknown.

    This is the distinction the whole phase exists for. Read naively, "no
    verified event in the log" means the meeting was not created, and the run
    reports failure. That is a claim the durable record does not support: the
    write may well have landed and lost its receipt.
    """
    assert (
        _verdict(_lost_answer(error_code=error_code))
        is FulfillmentVerdict.INDETERMINATE
    )


def test_a_provider_refusal_is_still_not_fulfilled() -> None:
    """The unknown must not swallow failures that really are failures.

    ``PROVIDER_ERROR`` means the provider answered, and a negative answer to a
    write means the write did not take effect. Reporting that as undecidable
    would be the same error in the opposite direction: manufacturing doubt where
    the record is certain, and hiding a real defect behind a human.

    Note the ``external_outcome=None``: the runtime only writes the marker when it
    actually concluded the world was unestablished, so a known refusal carries no
    marker at all. That absence is the signal, which is why the fixture has to
    clear it deliberately.
    """
    assert (
        _verdict(_lost_answer(error_code=PROVIDER_ERROR, external_outcome=None))
        is FulfillmentVerdict.NOT_FULFILLED
    )


def test_a_write_that_never_reached_the_provider_is_not_fulfilled() -> None:
    """A connection that was never established is a *known* non-attempt.

    ``PROVIDER_NOT_CONTACTED`` is the strongest negative evidence the system can
    hold about a write: nothing left the process. Parking it would tell a human
    the world is undecidable when KAI knows the request was never sent.
    """
    assert (
        _verdict(_lost_answer(error_code=PROVIDER_NOT_CONTACTED, external_outcome=None))
        is FulfillmentVerdict.NOT_FULFILLED
    )


def test_a_read_that_lost_its_answer_does_not_make_the_verdict_unknown() -> None:
    """Only a write's unknown is undecidable; a read's is just retryable.

    The record below belongs to no witnessed tool, so it is not an undetermined
    creation. Nothing about the *meeting* became undecidable \u2014 only a lookup did.
    """
    assert (
        _verdict(_lost_answer(tool="calendar.read_slots", external_outcome=None))
        is FulfillmentVerdict.NOT_FULFILLED
    )


def test_an_unverified_creation_with_an_unknown_outcome_is_indeterminate() -> None:
    """The provider answered but could not confirm it \u2014 still not proof of absence.

    Read-after-create that finds nothing is an answer the *verification boundary*
    gave, but it does not say the event does not exist; it says the event could
    not be vouched for. Paired with an unestablished outcome the honest verdict is
    the third one.
    """
    record = _lost_answer(outcome="EXECUTION_SUCCEEDED", error_code=PROVIDER_UNREACHABLE)
    assert _verdict(record) is FulfillmentVerdict.INDETERMINATE


def test_a_proven_witness_outranks_an_unknown_outcome() -> None:
    """Proof settles the question even when another attempt never concluded.

    The question is whether such a meeting *exists*, not whether every attempt
    finished. Once a witness satisfies every criterion, the unknown cannot
    un-prove it, and reporting INDETERMINATE here would park a task that is
    demonstrably done.
    """
    assert (
        _verdict(_created(), _lost_answer())
        is FulfillmentVerdict.FULFILLED
    )
    assert (
        _verdict(_lost_answer(), _created())
        is FulfillmentVerdict.FULFILLED
    )


def test_a_witness_on_the_wrong_day_does_not_outrank_an_unknown_outcome() -> None:
    """Proof only counts when it satisfies the criteria.

    A qualifying creation that booked the wrong day proves the provider responded
    and that *this* meeting was not created. An unknown remains possible, so the
    verdict cannot collapse to either certainty.
    """
    assert (
        _verdict(_misplaced_creation(), _lost_answer())
        is FulfillmentVerdict.INDETERMINATE
    )


def test_the_unknown_is_never_itself_a_witness() -> None:
    """An unestablished record cannot be promoted into evidence that it happened.

    The record that reports an unknown carries no provider event id. If it could
    qualify, a run that reached nothing would be able to satisfy the ``event_id``
    criterion with its own failure.
    """
    from kai.runtime.evidence import qualifying_creations, undetermined_creations

    record = _lost_answer()
    assert qualifying_creations([record]) == ()
    assert len(undetermined_creations([record])) == 1


def test_no_relevant_evidence_at_all_is_still_not_fulfilled() -> None:
    """Indeterminacy needs an unknown; silence on its own is a decision.

    An empty log means nothing was attempted, which is decidable, and reading it
    as INDETERMINATE would make the third value the default for every run that
    never started.
    """
    assert _verdict() is FulfillmentVerdict.NOT_FULFILLED


def test_an_unknown_outcome_is_not_a_vacuous_fulfillment() -> None:
    """The empty-set-is-not-fulfilled rule still binds when nothing is known."""
    assert _verdict(_lost_answer()) is not FulfillmentVerdict.FULFILLED


def test_the_verdict_is_invariant_to_where_the_unknown_sits() -> None:
    """Undeterminacy is a property of the set, not of the log's order."""
    first = _lost_answer()
    second = _lost_answer(error_code=PROVIDER_UNREACHABLE)
    verdicts = {
        _verdict(first, second),
        _verdict(second, first),
    }
    assert verdicts == {FulfillmentVerdict.INDETERMINATE}


@pytest.mark.parametrize("marker", [42, "unknown", "UNKNOWN ", None, True, []])
def test_only_the_exact_marker_value_makes_a_record_unknown(marker: object) -> None:
    """The marker is matched exactly; nothing near-miss counts as unknown.

    ``external_outcome`` is compared against one literal. A malformed or
    near-miss value is treated as absent rather than coerced, because coercing it
    would let a corrupt record manufacture doubt about a write that the record
    otherwise says nothing about.
    """
    record = _lost_answer(error_code=APPROVAL_STALE, external_outcome=marker)
    expected = (
        FulfillmentVerdict.INDETERMINATE
        if marker == EXTERNAL_OUTCOME_UNKNOWN
        else FulfillmentVerdict.NOT_FULFILLED
    )
    assert _verdict(record) is expected


# --- the verdict is a pure function of the durable inputs ------------------------


def test_satisfied_criteria_are_fulfilled() -> None:
    assert evaluate_calendar_meeting(_inputs(_created())) is FulfillmentVerdict.FULFILLED


def test_attendee_comparison_is_case_insensitive() -> None:
    """``Ahmed@Example.com`` was resolved; the event carries it folded."""
    assert evaluate_calendar_meeting(_inputs(_created())) is FulfillmentVerdict.FULFILLED


def test_no_created_event_is_not_fulfilled() -> None:
    assert evaluate_calendar_meeting(_inputs()) is FulfillmentVerdict.NOT_FULFILLED


def test_an_unverified_creation_is_not_fulfilled() -> None:
    entry = _created(verified=False)
    assert evaluate_calendar_meeting(_inputs(entry)) is FulfillmentVerdict.NOT_FULFILLED


def test_a_missing_event_id_is_not_fulfilled() -> None:
    """A success the provider never confirmed is not a meeting."""
    entry = _created(evidence={})
    assert evaluate_calendar_meeting(_inputs(entry)) is FulfillmentVerdict.NOT_FULFILLED


def test_an_unresolved_attendee_is_not_fulfilled() -> None:
    """The event went out, but not to everyone the Intent named."""
    entry = _created(args={"date": "2026-06-24", "start_time": "17:30", "attendees": []})
    assert evaluate_calendar_meeting(_inputs(entry)) is FulfillmentVerdict.NOT_FULFILLED


def test_an_extra_attendee_is_allowed() -> None:
    """Coverage is ``resolved <= attendees``, not equality: a plus-one is fine."""
    entry = _created(
        args={
            "date": "2026-06-24",
            "start_time": "17:30",
            "attendees": ["ahmed@example.com", "someone.else@example.com"],
        }
    )
    assert evaluate_calendar_meeting(_inputs(entry)) is FulfillmentVerdict.FULFILLED


def test_an_event_outside_the_window_is_not_fulfilled() -> None:
    entry = _created(
        args={
            "date": "2026-06-30",
            "start_time": "17:30",
            "attendees": ["ahmed@example.com"],
        }
    )
    assert evaluate_calendar_meeting(_inputs(entry)) is FulfillmentVerdict.NOT_FULFILLED


def test_an_event_before_the_earliest_time_is_not_fulfilled() -> None:
    entry = _created(
        args={
            "date": "2026-06-24",
            "start_time": "15:59",
            "attendees": ["ahmed@example.com"],
        }
    )
    assert evaluate_calendar_meeting(_inputs(entry)) is FulfillmentVerdict.NOT_FULFILLED


def test_unparseable_args_are_not_fulfilled_rather_than_an_error() -> None:
    entry = _created(
        args={
            "date": "not-a-date",
            "start_time": "17:30",
            "attendees": ["ahmed@example.com"],
        }
    )
    assert evaluate_calendar_meeting(_inputs(entry)) is FulfillmentVerdict.NOT_FULFILLED


# --- evidence set semantics ---------------------------------------------------
#
# The defect these close: the verdict used to be a function of the *first*
# qualifying record in the log, so the same logical evidence logged in a
# different order could produce a different answer.


def test_same_logical_evidence_in_a_different_order_gives_the_same_verdict() -> None:
    """The regression this phase exists for, stated directly.

    Two qualifying creations — one on the wrong day, one on the right one. Under
    first-witness semantics the order decided the answer, and reading the same
    two records the other way round flipped the task between ``COMPLETED`` and
    ``FAILED``. Existence does not depend on which record the log put first.
    """
    good = _created()
    bad = _misplaced_creation()
    assert _verdict(good, bad) is FulfillmentVerdict.FULFILLED
    assert _verdict(bad, good) is FulfillmentVerdict.FULFILLED


def test_every_permutation_of_the_relevant_set_gives_one_verdict() -> None:
    """Invariance is checked over all orderings, not on two hand-picked ones."""
    records = [
        _created(),
        _misplaced_creation(),
        _created(args={"attendees": ["nobody@example.com"]}),
    ]
    orders = _permutations(records)
    assert len(orders) == 6, "the fixture should enumerate every ordering"
    assert {_verdict(*order) for order in orders} == {FulfillmentVerdict.FULFILLED}


def test_every_permutation_of_a_failing_set_gives_one_verdict() -> None:
    """Order independence must hold for a failing set too, not only a passing one."""
    records = [
        _misplaced_creation(),
        _misplaced_creation(evidence={"event_id": "evt-also-wrong"}),
        _created(args={"start_time": "15:00", "attendees": ["ahmed@example.com"]}),
    ]
    orders = _permutations(records)
    assert len(orders) == 6
    assert {_verdict(*order) for order in orders} == {FulfillmentVerdict.NOT_FULFILLED}


def test_a_duplicate_witness_does_not_change_the_verdict() -> None:
    """The same meeting recorded twice is one meeting.

    A resumed run can re-record a creation. With no representative chosen out of
    the set, a repeat simply cannot be counted twice or counted at all.
    """
    once = _created()
    assert _verdict(once) is FulfillmentVerdict.FULFILLED
    assert _verdict(once, once) is FulfillmentVerdict.FULFILLED
    assert _verdict(once, once, once) is FulfillmentVerdict.FULFILLED

    failing = _misplaced_creation()
    assert _verdict(failing) is FulfillmentVerdict.NOT_FULFILLED
    assert _verdict(failing, failing) is FulfillmentVerdict.NOT_FULFILLED


def test_duplicates_do_not_depend_on_where_they_land() -> None:
    bad = _misplaced_creation()
    good = _created()
    assert _verdict(good, bad, bad) is FulfillmentVerdict.FULFILLED
    assert _verdict(bad, good, bad) is FulfillmentVerdict.FULFILLED
    assert _verdict(bad, bad, good) is FulfillmentVerdict.FULFILLED


def test_irrelevant_evidence_does_not_alter_the_verdict() -> None:
    """Evidence that is not about this question must not be able to vote on it.

    A failed attempt, another tool, an unverified result and a success with no
    provider id are all real records in the log and none of them is evidence
    about whether a qualifying meeting was created.
    """
    irrelevant: list[dict[str, Any]] = [
        _created(ok=False),
        _created(verified=False),
        _created(outcome="EXECUTION_FAILED"),
        _created(tool="calendar.read_slots"),
        _created(tool="contacts.match"),
        _created(tool="gmail.send"),
        _created(evidence={}),
        _created(evidence={}, data={"event_id": "evt-from-data-only"}),
        {"tool": "calendar.create_event"},
        {"junk": True},
    ]
    for record in irrelevant:
        assert _verdict(_created(), record) is FulfillmentVerdict.FULFILLED, record
        assert _verdict(record, _created()) is FulfillmentVerdict.FULFILLED, record
        assert _verdict(record) is FulfillmentVerdict.NOT_FULFILLED, record


def test_irrelevant_evidence_cannot_rescue_a_failing_set() -> None:
    """Nor can it manufacture fulfillment out of nothing."""
    bad = _misplaced_creation()
    noise = [_created(tool="calendar.read_slots"), _created(ok=False)]
    assert _verdict(bad, *noise) is FulfillmentVerdict.NOT_FULFILLED
    assert _verdict(*noise) is FulfillmentVerdict.NOT_FULFILLED


def test_a_retrying_run_is_judged_on_the_creation_that_actually_happened() -> None:
    """The shape the runtime already produces: a failed attempt, then a good one.

    ``test_branching_alternative_paths`` records exactly this — two
    ``calendar.create_event`` records in one run, the first not ``ok``. The failed
    one is not relevant evidence, so the answer is the second record's alone.
    """
    failed = _created(ok=False, evidence={"event_id": "evt-attempt"})
    good = _created()
    assert _verdict(failed, good) is FulfillmentVerdict.FULFILLED
    assert _verdict(good, failed) is FulfillmentVerdict.FULFILLED


def test_multiple_witnesses_follow_one_explicit_rule() -> None:
    """The rule, named: existence, not unanimity and not first-found.

    The question is whether a meeting satisfying every criterion exists, so one
    qualifying witness that satisfies them is enough. Unanimity would answer a
    different question — "is every meeting the run created correct?" — and would
    be a new domain rule with nothing in the repository to support it.
    """
    good = _created()
    wrong_day = _misplaced_creation()
    wrong_people = _created(args={"attendees": ["stranger@example.com"]})
    too_early = _created(args={"start_time": "09:00", "attendees": ["ahmed@example.com"]})

    # Existence: one correct witness among incorrect ones is enough.
    assert _verdict(wrong_day, good) is FulfillmentVerdict.FULFILLED
    assert _verdict(good, wrong_day, wrong_people, too_early) is FulfillmentVerdict.FULFILLED
    # And with no correct witness at all, existence fails.
    assert _verdict(wrong_day, wrong_people, too_early) is FulfillmentVerdict.NOT_FULFILLED
    # The rule is existential and not "first found": moving the good witness to
    # the end of the log must not change either answer.
    assert _verdict(wrong_day, wrong_people, good) is FulfillmentVerdict.FULFILLED


def test_the_empty_relevant_set_is_not_fulfilled_and_never_vacuously_true() -> None:
    """An empty set is an answer: nothing was created, so nothing was fulfilled.

    Guarding this explicitly is the point. An empty witness set reaching an
    ``all()`` would read as vacuous truth and mark a task ``COMPLETED`` on a run
    that booked nothing — the precise failure this phase must not introduce.
    """
    assert _verdict() is FulfillmentVerdict.NOT_FULFILLED
    assert _verdict(*[_created(ok=False) for _ in range(3)]) is (
        FulfillmentVerdict.NOT_FULFILLED
    )
    # Never INDETERMINATE: absence of a creation is decidable, not a gap.
    assert _verdict() is not FulfillmentVerdict.FULFILLED
    assert _verdict() is not FulfillmentVerdict.INDETERMINATE


def test_a_single_witness_is_judged_exactly_as_before() -> None:
    """One record in the set means the verdict is that record's verdict.

    With a single relevant witness the existential rule and the old
    first-witness rule are the same function, so no existing calendar meaning
    moved. Pinned case by case against the original outcomes.
    """
    cases: list[tuple[dict[str, Any], FulfillmentVerdict]] = [
        (_created(), FulfillmentVerdict.FULFILLED),
        (_created(verified=False), FulfillmentVerdict.NOT_FULFILLED),
        (_created(ok=False), FulfillmentVerdict.NOT_FULFILLED),
        (_created(evidence={}), FulfillmentVerdict.NOT_FULFILLED),
        (_created(tool="calendar.read_slots"), FulfillmentVerdict.NOT_FULFILLED),
        (
            _created(args={"date": "2026-06-30", "start_time": "17:30"}),
            FulfillmentVerdict.NOT_FULFILLED,
        ),
        (
            _created(args={"date": "2026-06-24", "start_time": "15:59"}),
            FulfillmentVerdict.NOT_FULFILLED,
        ),
        (
            _created(args={"date": "2026-06-24", "start_time": "17:30",
                           "attendees": []}),
            FulfillmentVerdict.NOT_FULFILLED,
        ),
        (
            _created(args={"date": "not-a-date", "start_time": "17:30"}),
            FulfillmentVerdict.NOT_FULFILLED,
        ),
    ]
    for record, expected in cases:
        assert _verdict(record) is expected, record


def test_the_verdict_survives_a_permuted_replay_of_a_whole_log() -> None:
    """The end-to-end shape: a realistic log, read back in a scrambled order.

    This is what recovery and re-derivation actually do — re-read the durable
    evidence — so it is the property the rest of the system rests on.
    """
    log: list[dict[str, Any]] = [
        _created(ok=False, evidence={"event_id": "evt-attempt-1"}),
        _created(tool="contacts.match", outcome="EXECUTION_SUCCEEDED"),
        _created(tool="calendar.read_slots", outcome="EXECUTION_SUCCEEDED"),
        _misplaced_creation(),
        _created(),
        _created(tool="calendar.get_event", outcome="EXECUTION_SUCCEEDED"),
    ]
    orders = _permutations(log)
    assert len(orders) == 720, "every ordering of a six-record log"
    assert {_verdict(*order) for order in orders} == {FulfillmentVerdict.FULFILLED}


def test_the_evaluator_input_is_frozen() -> None:
    """An evaluator cannot be handed a session, a task, or a live model response."""
    inputs = _inputs(_created())
    with pytest.raises(FrozenInstanceError):
        inputs.evidence = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        inputs.criteria = _criteria()  # type: ignore[misc]
    # The scenario and identities are gone from the input on purpose: they are what
    # the criteria were sealed from, and passing them too would invite the
    # inference the typed model exists to remove.
    assert set(FulfillmentInput.__dataclass_fields__) == {"criteria", "evidence"}


def test_the_evaluator_is_total_and_never_raises() -> None:
    """Every verdict is one of the three values, for any durable log at all."""
    verdicts = {
        evaluate_calendar_meeting(_inputs(_created())),
        evaluate_calendar_meeting(_inputs()),
        evaluate_calendar_meeting(_inputs({"tool": "calendar.create_event"})),
        evaluate_calendar_meeting(_inputs({"junk": object()})),
    }
    assert verdicts <= set(FulfillmentVerdict)


def test_the_evaluator_returns_no_value_outside_the_three() -> None:
    assert {v.value for v in FulfillmentVerdict} == {
        "FULFILLED",
        "NOT_FULFILLED",
        "INDETERMINATE",
    }


def test_the_same_inputs_always_give_the_same_verdict() -> None:
    """This is what lets a resumed run re-derive the decision it lost."""
    inputs = _inputs(_created())
    assert [evaluate_calendar_meeting(inputs) for _ in range(3)] == [
        FulfillmentVerdict.FULFILLED
    ] * 3


# --- the sealed contract is what selects the evaluator --------------------------


def _task_row(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "goal": "schedule a meeting",
        "fulfillment_profile": "calendar_meeting",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _scenario() -> Any:
    return SimpleNamespace(window=WINDOW)


def test_the_calendar_profile_reaches_the_calendar_evaluator() -> None:
    outcome = _fulfillment_outcome(
        _task_row(),
        scenario=_scenario(),
        identities=list(IDENTITIES),
        evidence=[_created()],
    )
    assert outcome.verdict is FulfillmentVerdict.FULFILLED


def test_an_unknown_profile_is_refused_rather_than_guessed() -> None:
    """No fallback evaluator: an unrecognised profile stops the run."""
    with pytest.raises(RuntimeFatal, match="no fulfillment evaluator"):
        _fulfillment_outcome(
            _task_row(fulfillment_profile="invoice_paid"),
            scenario=_scenario(),
            identities=list(IDENTITIES),
            evidence=[_created()],
        )


def test_the_verdict_is_a_verdict_and_not_a_state() -> None:
    """The evaluator never names a lifecycle state; it names an answer.

    The mapping to ``COMPLETED`` / ``FAILED`` / ``REQUIRES_ATTENTION`` belongs to
    the task state machine alone, which is the only thing allowed to move a
    task. Keeping the two vocabularies apart is what stops an evaluator from
    becoming an authority.
    """
    lifecycle = {
        "COMPLETED",
        "FAILED",
        "REQUIRES_ATTENTION",
        "WAITING_APPROVAL",
        "WAITING_EXTERNAL",
        "EXECUTING",
        "VERIFYING",
    }
    assert lifecycle.isdisjoint({v.value for v in FulfillmentVerdict})


# --- evaluator identity: what produced the verdict -----------------------------


def test_the_outcome_names_the_profile_the_evaluator_and_its_version() -> None:
    """One call yields all three, so the recorded triple cannot disagree.

    The profile and the version come from the same branch that ran the
    evaluator, so recording them is a description of what happened rather than a
    second lookup that has to agree with the first.
    """
    outcome = _fulfillment_outcome(
        _task_row(),
        scenario=_scenario(),
        identities=list(IDENTITIES),
        evidence=[_created()],
    )
    assert outcome.profile == "calendar_meeting"
    assert outcome.version == "calendar_meeting:v1"
    assert outcome.version.startswith(f"{outcome.profile}:")
    assert outcome.verdict is FulfillmentVerdict.FULFILLED


def test_the_reported_profile_is_the_sealed_one_not_a_re_derived_one() -> None:
    """The recorded profile is whatever the contract sealed, carried through.

    Not resolved from ``allowed_tools``, the goal, or the criteria text: those
    are inputs a resolver could disagree with the contract about, and the
    contract is the authority.
    """
    outcome = _fulfillment_outcome(
        _task_row(allowed_tools=["calendar", "gmail"], goal="pay an invoice"),
        scenario=_scenario(),
        identities=list(IDENTITIES),
        evidence=[_created()],
    )
    assert outcome.profile == "calendar_meeting"


def test_the_version_is_a_declared_constant_and_stable_across_evaluations() -> None:
    """The same evaluator and the same semantics always name themselves alike."""
    evaluations = [
        _fulfillment_outcome(
            _task_row(),
            scenario=_scenario(),
            identities=list(IDENTITIES),
            evidence=[entry],
        )
        for entry in (_created(), _created(args={"date": "2026-06-30"}), _created())
    ]
    assert len({e.version for e in evaluations}) == 1
    assert evaluations[0].version == CALENDAR_MEETING_EVALUATOR_VERSION
    # Re-deriving across separate calls, as a resumed run does, changes nothing.
    assert (
        _fulfillment_outcome(
            _task_row(),
            scenario=_scenario(),
            identities=list(IDENTITIES),
            evidence=[_created()],
        ).version
        == evaluations[0].version
    )


def test_the_version_is_deterministic_and_carries_no_run_state() -> None:
    """A version that moved per run would make the audit record meaningless.

    Pinned against the things it must not depend on: the clock, the process, and
    the environment. It is a literal, so it is identical everywhere, every time.
    """
    assert isinstance(CALENDAR_MEETING_EVALUATOR_VERSION, str)
    assert CALENDAR_MEETING_EVALUATOR_VERSION == "calendar_meeting:v1"
    # The suffix is a hand-declared semantic version, not a commit, a build, or
    # a clock: no long hex digest, no date, no millisecond counter.
    suffix = CALENDAR_MEETING_EVALUATOR_VERSION.split(":", 1)[1]
    assert re.fullmatch(r"v\d+", suffix), suffix
    assert not re.search(r"[0-9a-f]{7,}", suffix)
    assert len(CALENDAR_MEETING_EVALUATOR_VERSION) < 64, "it fits the column"


def test_the_version_is_bound_to_the_semantics_and_not_to_the_verdict() -> None:
    """One version covers all three verdicts, because it names the semantics.

    A version that changed with the answer would identify the outcome rather than
    the reasoning, and a historical row could not be read as "which rules were
    applied". The verdict is the variable; the version is the fixed evaluator.
    """
    versions = {
        _fulfillment_outcome(
            _task_row(),
            scenario=_scenario(),
            identities=list(IDENTITIES),
            evidence=[evidence],
        ).version
        for evidence in (
            _created(),
            _created(ok=False),
            _created(args={"date": "2026-06-30"}),
        )
    }
    assert versions == {CALENDAR_MEETING_EVALUATOR_VERSION}


def test_an_unknown_profile_records_no_identity_at_all() -> None:
    """Fail-closed: no verdict, so no identity, and no silent fallback.

    Refusing is the existing dispatch's behaviour and is not widened here. The
    point this pins is that the audit record can only ever describe a dispatch
    that actually ran.
    """
    with pytest.raises(RuntimeFatal):
        _fulfillment_outcome(
            _task_row(fulfillment_profile="invoice_paid"),
            scenario=_scenario(),
            identities=list(IDENTITIES),
            evidence=[_created()],
        )


def test_the_outcome_is_frozen_so_the_record_cannot_be_edited_afterwards() -> None:
    """A durable audit record that can be mutated in place is not a record."""
    outcome = FulfillmentOutcome(
        profile="calendar_meeting",
        version=CALENDAR_MEETING_EVALUATOR_VERSION,
        verdict=FulfillmentVerdict.FULFILLED,
    )
    with pytest.raises(FrozenInstanceError):
        outcome.verdict = FulfillmentVerdict.NOT_FULFILLED  # type: ignore[misc]
    assert set(FulfillmentOutcome.__dataclass_fields__) == {
        "profile",
        "version",
        "verdict",
    }


def test_the_recorded_verdict_is_from_the_existing_three_and_adds_none() -> None:
    """A fourth verdict would be a new state machine wearing a new hat."""
    recorded = {
        FulfillmentVerdict.FULFILLED,
        FulfillmentVerdict.NOT_FULFILLED,
        FulfillmentVerdict.INDETERMINATE,
    }
    assert recorded == set(FulfillmentVerdict)
    assert {v.value for v in FulfillmentVerdict} == {
        "FULFILLED",
        "NOT_FULFILLED",
        "INDETERMINATE",
    }


# --- the typed success criteria: what is proven, read from the schema ----------
#
# The gap these close: the evaluator used to receive a window and a bag of
# identities and know — from its own implementation — that those were the
# conditions. The meaning lived in the reading of the code. It now lives in a
# value.


def test_the_criteria_state_the_conditions_the_evaluator_checks() -> None:
    """Reading the model is enough to know what fulfillment requires.

    No prose and no implementation required: the conditions are named fields with
    types, and a reader can state the success question from this alone.
    """
    criteria = _criteria()
    assert criteria.profile == "calendar_meeting"
    payload = criteria.for_calendar_meeting()
    assert set(type(payload).model_fields) == {"window", "required_attendees"}
    assert payload.window is WINDOW
    assert payload.required_attendees == frozenset({"ahmed@example.com"})


def test_the_criteria_reuse_the_sealed_window_rather_than_copying_it() -> None:
    """One validated window object, not a second date/time copy to drift.

    A duplicated ``start_date``/``end_date``/``earliest_time`` set would be a
    second thing that could disagree with the sealed one, and the evaluator would
    no longer be reading the window the run was planned against.
    """
    assert _criteria().for_calendar_meeting().window is WINDOW


def test_an_anchor_plus_a_derived_value_is_the_licensed_shape() -> None:
    """The invariant licenses ``anchor + derived``, not ``anchor only``.

    ``required_attendees`` is derived from the resolved identity row, and the
    calendar profile still carries a sealed-intent anchor (the enforced window)
    that its evaluator decides on. The rule is that *at least one* criterion
    value must have a sealed user-intent source — it does not make derived
    values invalid, so a profile with both halves stays live-licensable and
    still evaluates exactly as before.
    """
    from kai.tasks.service import LIVE_SEALABLE_PROFILES

    payload = _criteria().for_calendar_meeting()
    assert payload.window is WINDOW  # sealed-intent anchor, evaluator-decided
    assert payload.required_attendees == frozenset({"ahmed@example.com"})  # derived
    assert "calendar_meeting" in LIVE_SEALABLE_PROFILES
    assert (
        evaluate_calendar_meeting(_inputs(_created()))
        is FulfillmentVerdict.FULFILLED
    )


def test_the_criteria_carry_only_what_the_evaluator_reads() -> None:
    """``title`` is in the window but is not a success condition.

    It is a label for the created event. Promoting it into the criteria because it
    happens to be available would invent a requirement the domain never had, and it
    is asserted here so it cannot creep in.
    """
    payload = _criteria().for_calendar_meeting()
    assert "title" in type(payload.window).model_fields
    assert "title" not in set(type(payload).model_fields)
    assert ".title" not in _evaluator_source(evaluate_calendar_meeting)
    assert ".title" not in _evaluator_source(_meeting_satisfies_criteria)


def test_the_third_prose_criterion_is_not_a_field() -> None:
    """"an event was created" is the evidence question, not a parameter.

    It is answered by the existence of a qualifying creation, which is the
    evaluator's quantifier. A boolean field here would be a second thing able to
    disagree with the evidence, and would let a criteria claim creation without any
    creation existing.
    """
    assert set(CalendarMeetingCriteria.model_fields) == {
        "window",
        "required_attendees",
    }


def test_the_criteria_are_sealed_against_mutation() -> None:
    """Frozen, so a sealed criteria cannot be widened after the fact."""
    criteria = _criteria()
    with pytest.raises(ValidationError):
        criteria.calendar_meeting.required_attendees = (  # type: ignore[misc]
            frozenset({"anyone@example.com"})
        )
    with pytest.raises(ValidationError):
        criteria.calendar_meeting.window = WINDOW  # type: ignore[misc]
    with pytest.raises(ValidationError):
        criteria.profile = "calendar_meeting"  # type: ignore[misc]
    assert _criteria() == criteria


def test_the_criteria_reject_fields_outside_the_domain() -> None:
    """``extra="forbid"``, so nothing can be attached that no one will read."""
    with pytest.raises(ValidationError):
        CalendarMeetingCriteria(  # type: ignore[call-arg]
            window=WINDOW, expression="attendee and (date or time)"
        )
    with pytest.raises(ValidationError):
        SuccessCriteria(  # type: ignore[call-arg]
            profile="calendar_meeting",
            calendar_meeting=_criteria().for_calendar_meeting(),
            expression="attendee and (date or time)",
        )


def test_the_criteria_reject_a_malformed_window_at_the_sealing_boundary() -> None:
    """An invalid window fails while being sealed, not inside the evaluator.

    ``MeetingWindow`` is the existing validation boundary; the criteria inherit it
    rather than restating it, so a bad window is rejected where the window is
    validated — and never reaches a comparison against ``args["date"]``.
    """
    with pytest.raises(ValidationError):
        MeetingWindow(start_date=date(2026, 6, 26), end_date=date(2026, 6, 22))
    with pytest.raises(ValidationError):
        MeetingWindow(
            start_date=date(2026, 1, 1), end_date=date(2026, 6, 22)
        )  # over the 14-day bound
    with pytest.raises(ValidationError):
        calendar_meeting_criteria(
            profile="calendar_meeting",
            window={"start_date": "2026-06-22"},  # type: ignore[arg-type]
            identities=IDENTITIES,
        )


def test_a_malformed_identity_coerces_exactly_as_before_rather_than_being_refused() -> None:
    """A new rejection here would change an already-decided case.

    The prose-era evaluator built its required set with ``str(item["email"])``, so
    an email that was not a string became its textual form and simply failed to
    match any attendee. Raising instead would turn ``NOT_FULFILLED`` into a failed
    run, which is a change of meaning rather than a validation — so the total
    coercion is kept and pinned.
    """
    weird = [{"email": ["not", "a", "string"]}, {"email": 42}]
    payload = _criteria(identities=weird).for_calendar_meeting()
    assert payload.required_attendees == frozenset(
        {"['not', 'a', 'string']", "42"}
    )
    # And so the verdict is the one the old code gave: nothing matches.
    assert (
        evaluate_calendar_meeting(
            FulfillmentInput(
                criteria=_criteria(identities=weird), evidence=(_created(),)
            )
        )
        is FulfillmentVerdict.NOT_FULFILLED
    )


def test_the_criteria_refuse_a_profile_they_cannot_back() -> None:
    """The criteria cannot name a question they carry no conditions for.

    This is the closed vocabulary, stated as a check: one profile, one payload. A
    criteria object claiming a different question would have to invent conditions,
    and it is refused at construction instead of meeting an evaluator that does not
    exist.
    """
    with pytest.raises(ValidationError):
        SuccessCriteria(  # type: ignore[call-arg]
            profile="invoice_paid",
            calendar_meeting=_criteria().for_calendar_meeting(),
        )
    impostor = _criteria().model_copy(update={"profile": "invoice_paid"})
    with pytest.raises(ValueError, match="no calendar criteria"):
        impostor.for_calendar_meeting()


def test_the_criteria_normalize_attendees_exactly_as_the_evaluator_always_did() -> None:
    """The required set is the same set the prose-era code compared against.

    Case-folded, and skipping identities with no email — including the empty string
    the resolver can produce. A change here would be a change to what "the requested
    contact" means, so the normalization is pinned rather than re-decided.
    """
    identities = [
        {"email": "Ahmed@Example.com"},
        {"email": "SAIF@example.com"},
        {"email": ""},
        {"email": None},
        {"name": "no email at all"},
    ]
    payload = _criteria(identities=identities).for_calendar_meeting()
    assert payload.required_attendees == frozenset(
        {"ahmed@example.com", "saif@example.com"}
    )


def test_an_unresolved_attendee_set_is_sealed_as_empty_not_refused() -> None:
    """An empty required set is a legitimate sealed value, not a malformed one.

    ``test_an_unresolved_attendee_is_not_fulfilled`` already decides this case as
    ``NOT_FULFILLED``. Rejecting it here would convert a decided verdict into a
    failed run, which is a change of meaning and not a validation.
    """
    criteria = _criteria(identities=[{"name": "nobody resolved"}])
    assert criteria.for_calendar_meeting().required_attendees == frozenset()
    assert (
        evaluate_calendar_meeting(
            FulfillmentInput(criteria=criteria, evidence=(_created(),))
        )
        is FulfillmentVerdict.NOT_FULFILLED
    )


def test_sealing_the_criteria_is_deterministic() -> None:
    """Same sealed window and identities in, same criteria out — always.

    This is what lets a recovery rebuild the value a crashed run applied without
    any risk of landing somewhere else.
    """
    assert _criteria() == _criteria()
    assert _criteria().model_dump_json() == _criteria().model_dump_json()
    # Order of the input identities does not leak into the sealed value either.
    a = [{"email": "SAIF@example.com"}, {"email": "Ahmed@Example.com"}]
    b = list(reversed(a))
    assert _criteria(identities=a) == _criteria(identities=b)


def test_the_criteria_are_serializable_and_reloadable() -> None:
    """Round-trips through JSON, so a stored value could be read back as a value.

    Nothing in the runtime does this today — sealing is inherited from the sealed
    rows rather than from a second copy — but the model is a persisted-shaped value,
    and it is worth being able to prove it survives storage unchanged.
    """
    criteria = _criteria()
    assert SuccessCriteria.model_validate_json(criteria.model_dump_json()) == criteria


def test_the_human_rendering_is_a_projection_and_not_a_source() -> None:
    """Readable for audit, never read back.

    A sentence a human can read is convenient, and it is exactly the kind of value
    that gets parsed one day. It is derived from the fields, so it cannot state
    anything the criteria do not.
    """
    rendered = _criteria().for_calendar_meeting().as_criteria()
    assert "2026-06-22" in rendered and "2026-06-26" in rendered
    assert "16:00:00" in rendered
    assert "ahmed@example.com" in rendered
    # No evaluator read of it: the string form is not an input anywhere.
    assert set(FulfillmentInput.__dataclass_fields__) == {"criteria", "evidence"}


# --- the criteria are the authority; the prose is not --------------------------


def test_the_evaluator_reads_the_typed_model_and_never_the_prose() -> None:
    """The meaning comes from the value, not from a sentence about it.

    The strongest available statement: the evaluator's own source contains no
    reference to ``success_criteria``, to ``SLICE_SUCCESS_CRITERIA``, or to any
    condition phrased as text. What it compares is ``criteria.window`` and
    ``criteria.required_attendees``.
    """
    assert "success_criteria" not in _evaluator_source(evaluate_calendar_meeting)
    assert "SLICE_SUCCESS_CRITERIA" not in _evaluator_source(evaluate_calendar_meeting)
    assert "for_calendar_meeting" in _evaluator_source(evaluate_calendar_meeting)
    helper = _evaluator_source(_meeting_satisfies_criteria)
    assert "criteria.required_attendees" in helper
    assert "criteria.window" in helper


def test_changing_the_prose_after_sealing_changes_no_verdict() -> None:
    """The description is not an input, so rewriting it moves nothing.

    A sealed contract whose prose was edited — or a planner that paraphrased it,
    were the verbatim check relaxed — must not be able to reach a verdict. Every
    prose form, including one asserting the opposite, gives the same answer on the
    same evidence.
    """
    baseline = evaluate_calendar_meeting(_inputs(_created()))
    for text in (
        "calendar event created",
        "attendee is the requested contact",
        "event fits the requested time window",
        "an event was never created and must not count",
        "complete only if nothing was scheduled",
    ):
        contract = TaskContractInput(
            goal="schedule a meeting",
            success_criteria=[text],
            fulfillment_profile="calendar_meeting",
            fallback="ask the user",
        )
        assert contract.success_criteria == [text]
        assert evaluate_calendar_meeting(_inputs(_created())) is baseline, text
    # The same evidence, read against criteria that do not match it, is unchanged.
    assert (
        evaluate_calendar_meeting(_inputs(_created(), _misplaced_creation()))
        is FulfillmentVerdict.FULFILLED
    )
    assert (
        evaluate_calendar_meeting(_inputs(_misplaced_creation()))
        is FulfillmentVerdict.NOT_FULFILLED
    )


def test_the_criteria_and_the_prose_are_two_separate_surfaces() -> None:
    """Both exist, and only one is authority.

    The prose remains in the contract because the planner reproduces it verbatim;
    the typed criteria is what the evaluator reads. Keeping them distinct is the
    point — merging them is what this phase removed.
    """
    from kai.runtime.translator import (
        SLICE_FULFILLMENT_PROFILE,
        SLICE_SUCCESS_CRITERIA,
        build_slice_contract,
    )

    contract = build_slice_contract("schedule a meeting")
    assert contract.success_criteria == SLICE_SUCCESS_CRITERIA
    assert contract.fulfillment_profile == SLICE_FULFILLMENT_PROFILE
    # The contract carries no typed criteria field, and none is invented here: the
    # criteria are sealed from the sealed window and identities.
    assert not {
        "success_criteria_semantics",
        "criteria",
        "typed_success_criteria",
    } & set(type(contract).model_fields)
    # The prose states a third criterion the typed model deliberately omits,
    # because that one is answered by the evidence rather than by a parameter.
    assert "calendar event created" in SLICE_SUCCESS_CRITERIA
    assert "calendar event created" not in str(_criteria())


# --- the property this phase exists to establish ------------------------------


def test_the_verdict_is_a_function_of_sealed_criteria_and_evidence() -> None:
    """``Verdict = f(criteria, evidence)`` — and of nothing else.

    Each way a verdict must *not* be a function is closed here: not of the prose
    (not an input to anything below), not of live model output (``FulfillmentInput``
    structurally cannot carry a session or a model call), and not of the tool list
    (``allowed_tools`` is not a criteria field).
    """
    from kai.runtime.translator import SLICE_ALLOWED_TOOLS

    criteria = _criteria()
    good = _created()
    assert SLICE_ALLOWED_TOOLS, "the contract's tool list exists and is unread here"
    assert not {"allowed_tools", "tools"} & set(CalendarMeetingCriteria.model_fields)

    # Same sealed criteria + same evidence set => one verdict, repeatedly.
    for _ in range(3):
        assert (
            evaluate_calendar_meeting(
                FulfillmentInput(criteria=criteria, evidence=(good,))
            )
            is FulfillmentVerdict.FULFILLED
        )

    # Holding the evidence fixed, changing the sealed criteria changes the answer:
    # the criteria are genuinely an input, not decoration.
    later = MeetingWindow(
        start_date=date(2026, 6, 22),
        end_date=date(2026, 6, 26),
        earliest_time=time(18, 0),
    )
    assert (
        evaluate_calendar_meeting(
            FulfillmentInput(criteria=_criteria(window=later), evidence=(good,))
        )
        is FulfillmentVerdict.NOT_FULFILLED
    )
    # Holding the criteria fixed, changing the evidence changes the answer.
    assert (
        evaluate_calendar_meeting(FulfillmentInput(criteria=criteria, evidence=()))
        is FulfillmentVerdict.NOT_FULFILLED
    )


def test_the_runtime_builds_the_criteria_from_sealed_rows_and_dispatches() -> None:
    """The one place criteria are built, wired to the real dispatch.

    The sealed scenario window and the sealed identities go in; a typed criteria
    naming the contract's profile comes out, and the evaluator runs on it. The prose
    is not a parameter of this function, so it cannot be an input.
    """
    outcome = _fulfillment_outcome(
        _task_row(),
        scenario=SimpleNamespace(window=WINDOW),
        identities=[{"email": "Ahmed@Example.com"}],
        evidence=[_created()],
    )
    assert outcome.verdict is FulfillmentVerdict.FULFILLED
    assert outcome.profile == "calendar_meeting"
    assert outcome.version == CALENDAR_MEETING_EVALUATOR_VERSION


def test_a_recovery_seals_the_identical_criteria() -> None:
    """Re-sealing from the same durable rows gives an equal value.

    Recovery rebuilds the criteria rather than reading a stored copy, so this is
    what guarantees the resumed run evaluates the same question the live run did.
    """
    rows = [{"email": "Ahmed@Example.com", "name": "Ahmed"}]
    first = calendar_meeting_criteria(
        profile="calendar_meeting", window=WINDOW, identities=rows
    )
    resumed = calendar_meeting_criteria(
        profile="calendar_meeting",
        window=MeetingWindow.model_validate(WINDOW.model_dump(mode="json")),
        identities=[dict(item) for item in rows],
    )
    assert resumed == first
    assert resumed.model_dump_json() == first.model_dump_json()


def test_a_criteria_cannot_reach_a_fulfillment_criterion_it_does_not_name() -> None:
    """No generic path: a criteria object is only usable by its own domain.

    There is no registry to look a criteria up in, and ``for_calendar_meeting`` is
    the only way to reach a payload, so another domain's criteria cannot be read by
    this evaluator even by accident.
    """
    from kai.runtime import fulfillment as module

    exported = set(module.__all__)
    assert {"SuccessCriteria", "CalendarMeetingCriteria"} <= exported
    assert not {"resolve_evaluator", "evaluator_for", "registry", "evaluate"} & exported
    assert isinstance(_criteria().for_calendar_meeting(), CalendarMeetingCriteria)


def test_the_criteria_cannot_change_the_profile_they_are_evaluated_under() -> None:
    """The dispatcher reads the profile off the task row, so a criteria cannot.

    Naming a profile inside a criteria can only ever agree with the contract, never
    redirect: the runtime builds the criteria itself, from the profile the contract
    sealed. An unknown profile is refused before any criteria exist.
    """
    task = _task_row(fulfillment_profile="calendar_meeting")
    outcome = _fulfillment_outcome(
        task,
        scenario=SimpleNamespace(window=WINDOW),
        identities=[{"email": "ahmed@example.com"}],
        evidence=[_created()],
    )
    assert outcome.profile == task.fulfillment_profile
    with pytest.raises(RuntimeFatal, match="no fulfillment evaluator"):
        _fulfillment_outcome(
            _task_row(fulfillment_profile="invoice_paid"),
            scenario=SimpleNamespace(window=WINDOW),
            identities=[{"email": "ahmed@example.com"}],
            evidence=[_created()],
        )


def test_the_criteria_do_not_pick_the_evaluator_version() -> None:
    """The version stays the evaluator's to state, unchanged at ``v1``.

    The criteria say what must be proven; which semantics answer it is the
    evaluator's identity, and it is not reachable from the criteria — so no
    criteria field can become a way to change what version is recorded.
    """
    outcome = _fulfillment_outcome(
        _task_row(),
        scenario=SimpleNamespace(window=WINDOW),
        identities=[{"email": "ahmed@example.com"}],
        evidence=[_created()],
    )
    assert outcome.version == CALENDAR_MEETING_EVALUATOR_VERSION == "calendar_meeting:v1"
    for model in (CalendarMeetingCriteria, SuccessCriteria):
        assert not {"version", "evaluator_version", "semantics"} & set(model.model_fields)
    assert "v1" not in _criteria().model_dump_json()


def test_the_model_cannot_add_a_condition_by_describing_one() -> None:
    """A condition exists only as a field, so prose cannot introduce one.

    There is nowhere for an expression to go: unknown fields are refused, and the
    fields that exist are read by name. A task wanting a fourth condition has to
    amend the model, not phrase it.
    """
    with pytest.raises(ValidationError):
        CalendarMeetingCriteria.model_validate(
            {
                "window": WINDOW.model_dump(mode="json"),
                "required_attendees": ["ahmed@example.com"],
                "also_requires": "the title mentions the attendee",
            }
        )
    assert set(CalendarMeetingCriteria.model_fields) == {
        "window",
        "required_attendees",
    }


def test_the_dispatch_stays_a_closed_vocabulary_and_not_a_registry() -> None:
    """The dispatch is still written out, and now answers three closed questions.

    This began as a guard against the temptation to make the criteria generic
    *just in case* while only one domain existed. A second domain arrived — the
    ``gmail_send`` experiment — and then a third, the ``task_preparation`` local
    slice — and the temptation is the same and one step closer each time, because
    domains that *look* alike invite the rule that would generate the next one.
    So the guard is kept and re-aimed rather than deleted:

    * every evaluator is called once each, from an explicit branch — a registry
      would replace the counts with one lookup;
    * the forbidden machinery is still absent, so a resolution hook cannot be
      introduced quietly;
    * the vocabulary is still a ``Literal`` of exactly the three sealed questions.
      Asserting *three* rather than a lower bound is the point: the day a fourth
      profile appears without a deliberate edit here, this fails.
    """
    from typing import get_args

    from kai.tasks.schemas import FulfillmentProfile

    source = _code_of(_fulfillment_outcome)
    assert source.count("evaluate_calendar_meeting(") == 1
    assert source.count("evaluate_gmail_send(") == 1
    assert source.count("evaluate_task_preparation(") == 1
    assert source.count("calendar_meeting_criteria(") == 1
    assert source.count("gmail_send_criteria(") == 1
    assert source.count("task_preparation_criteria(") == 1
    for forbidden in ("importlib", "getattr(", "registry", "__subclasses__"):
        assert forbidden not in source
    # Every question the runtime can be asked is named, and nothing else is.
    assert set(get_args(FulfillmentProfile)) == {
        "calendar_meeting",
        "gmail_send",
        "task_preparation",
    }


def test_the_criteria_need_no_new_persistence() -> None:
    """Both criteria inputs already live on the execution row, durably.

    The reason this phase adds no column: the window is sealed in ``scenario`` and
    the identities in ``identities``, both committed before any tool runs and never
    recomputed. The criteria are sealed by inheritance, and re-derivable after a
    crash from the same rows — so there is no second copy to store, drift, or
    back-fill, and no historical row that lacks them.

    The second domain needed **no new storage either**, and that is a result
    rather than an assumption: ``gmail_send`` reads the same sealed ``identities``
    row and nothing else, so the experiment added a migration-free question. A
    third domain needing its own durable intent source would break this symmetry,
    and would be the first point at which a migration becomes justified by
    semantics instead of by taste.
    """
    from kai.runtime.models import RuntimeExecution

    persisted = set(RuntimeExecution.__table__.columns.keys())
    assert {"scenario", "identities"} <= persisted
    # Nothing about the criteria is stored separately, on purpose.
    assert not {"success_criteria", "criteria", "success_criteria_semantics"} & persisted
    # And no new column was added for this phase: the durable audit triple from
    # the previous phase is the only fulfillment storage on the row.
    assert {
        "fulfillment_profile",
        "evaluator_version",
        "fulfillment_verdict",
    } <= persisted


def test_the_criteria_carry_no_identifier_because_they_are_sealed_into_a_row() -> None:
    """A criteria value is not a row, so it holds no ids.

    Nothing to key it by, nothing to look it up with, and no way for it to be
    confused with the execution it was derived from.
    """
    fields = set(CalendarMeetingCriteria.model_fields) | set(SuccessCriteria.model_fields)
    assert not fields & {"id", "task_id", "tenant_id", "user_id", "run_id"}
    # The wrapper carries the profile and one slot per sealed question — and a
    # slot per *named* domain only, so a payload cannot arrive for a profile the
    # contract never sealed.
    assert set(SuccessCriteria.model_fields) == {
        "profile",
        "calendar_meeting",
        "gmail_send",
        "task_preparation",
    }


def test_the_criteria_cover_exactly_the_conditions_the_evaluator_enforces() -> None:
    """No condition in the model the evaluator ignores; none it needs missing.

    Read off the two sources and compared: every payload field is read by the
    evaluator, and every condition the evaluator applies is a field. A model field
    nothing reads is a promise the runtime does not keep; a check with no field is a
    meaning the schema cannot show anyone.
    """
    helper = _evaluator_source(_meeting_satisfies_criteria)
    for name in set(CalendarMeetingCriteria.model_fields):
        assert f"criteria.{name}" in helper, name
    # Both conditions, and only those two, are enforced over one meeting.
    assert "required <= attendees" in helper
    assert "window.start_date <= day <= window.end_date" in helper
    assert "start_time >= window.earliest_time" in helper
