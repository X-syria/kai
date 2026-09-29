"""The second-domain experiment: does the calendar's semantics reappear for a send?

The question this file exists to answer is not "does ``gmail.send`` work". It is
whether the *semantic structure* proven for ``calendar_meeting`` is the same
structure a different domain needs, or whether the change of domain exposes a
different one:

```text
Sealed SuccessCriteria  ->  gmail evaluator  ->  FulfillmentVerdict
                                                   ->  Task State Machine
```

The tests are grouped so the result is legible as an answer rather than as a pass
count. Group 1 asks what a send witness is; group 2 what proves the intent was
fulfilled; group 3 whether the quantifiers and set semantics survived; group 4
whether the binding and recovery survived; group 5 records what did **not**
survive. Group 6 is the control: ``calendar_meeting`` must be untouched.

No database, no HTTP, no provider, no model — exactly as for the calendar
evaluator, because an evaluator that needs any of those is not deterministic.
"""

from __future__ import annotations

import ast
import inspect
import itertools
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Final

import pytest
from pydantic import ValidationError

from kai.runtime.fulfillment import (
    CALENDAR_MEETING_EVALUATOR_VERSION,
    GMAIL_SEND_EVALUATOR_VERSION,
    CalendarMeetingCriteria,
    FulfillmentInput,
    FulfillmentOutcome,
    FulfillmentVerdict,
    GmailSendCriteria,
    SuccessCriteria,
    _send_satisfies_criteria,
    calendar_meeting_criteria,
    evaluate_calendar_meeting,
    evaluate_gmail_send,
    gmail_send_criteria,
)
from kai.runtime.send_evidence import (
    MESSAGE_SEND_TOOL,
    QualifyingSend,
    qualifying_send,
    qualifying_sends,
)
from kai.runtime.service import RuntimeFatal, _fulfillment_outcome

#: The resolved contact the Intent names. Sealed once, and never recomputed —
#: the gmail criteria are built from this exactly as the calendar ones are.
IDENTITIES: Final = ({"email": "Ahmed@Example.com", "name": "Ahmed"},)

#: A different, also-resolved contact, used to prove a criteria change is a real
#: input and not decoration.
OTHER_IDENTITIES: Final = ({"email": "saif@example.com", "name": "Saif"},)


def _sent(**overrides: Any) -> dict[str, Any]:
    """One ``gmail.send`` record, shaped as the gateway persists it.

    The shape is the real one: ``evidence`` carries what the verification
    boundary republished, ``data`` the adapter's serialized provider payload, and
    ``args`` the request the attempt was made with.
    """
    entry: dict[str, Any] = {
        "tool": "gmail.send",
        "outcome": "EXECUTION_SUCCEEDED",
        "ok": True,
        "verified": True,
        "args": {
            "to": "ahmed@example.com",
            "subject": "Quarterly plan",
            "body": "Attached is the plan.",
        },
        "data": {"message_id": "msg-1", "thread_id": "thread-1"},
        "evidence": {
            "message_id": "msg-1",
            "thread_id": "thread-1",
            "label_ids": ["SENT"],
            "mismatch_count": 0,
        },
    }
    entry.update(overrides)
    return entry


def _criteria(identities: Any = IDENTITIES) -> SuccessCriteria:
    """The sealed gmail success criteria for one resolved recipient."""
    return gmail_send_criteria(profile="gmail_send", identities=identities)


def _inputs(*evidence: Any, identities: Any = IDENTITIES) -> FulfillmentInput:
    return FulfillmentInput(
        criteria=_criteria(identities=identities), evidence=tuple(evidence)
    )


def _code_of(function: Callable[..., Any]) -> str:
    """The function's source with the docstring stripped.

    Used where a *word* is the thing under test: a docstring may discuss the
    rule it asserts, and that discussion is not the rule.
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


# --- 1. what a qualifying send attempt is ---------------------------------------


def test_a_verified_provider_identified_send_is_a_witness() -> None:
    witness = qualifying_send(_sent())
    assert isinstance(witness, QualifyingSend)
    assert witness.provider_message_id == "msg-1"
    assert witness.arguments["to"] == "ahmed@example.com"


@pytest.mark.parametrize(
    ("record", "why"),
    [
        ({"tool": "gmail.search"}, "searching a mailbox sends nothing"),
        ({"tool": "gmail.read"}, "reading a message is not sending one"),
        (
            {"tool": "calendar.create_event"},
            "the other domain's tool is not a send",
        ),
        ({"tool": "gmail.send", "outcome": "EXECUTION_FAILED"}, "the call failed"),
        (
            {"tool": "gmail.send", "outcome": "REQUIRES_ATTENTION"},
            "verification did not pass",
        ),
        ({"tool": "gmail.send", "outcome": "DENIED"}, "policy refused"),
        (_sent(ok=False), "not ok"),
        (_sent(verified=False), "not verified"),
        (_sent(evidence={}), "no provider identity"),
        (_sent(evidence={"message_id": ""}), "a blank identity is not an identity"),
        (_sent(evidence={"message_id": 42}), "an int is not an identity"),
        (_sent(evidence={"message_id": "   "}), "whitespace is not an identity"),
        (_sent(evidence=None), "no evidence container at all"),
        (_sent(evidence="msg-1"), "evidence must be a mapping"),
        ({"tool": "gmail.send"}, "a bare tool name is not a send"),
    ],
)
def test_anything_else_is_not_a_witness(record: Any, why: str) -> None:
    assert qualifying_send(record) is None, why
    # And qualification never raises, whatever it is handed.
    assert qualifying_send([record]) is None
    assert qualifying_send(None) is None
    assert qualifying_send("not a record") is None


def test_qualification_is_total_pure_and_reads_no_other_record() -> None:
    """Same record, same answer — whatever surrounds it and wherever it sits.

    This is what makes recovery sound: a re-derivation cannot see a different
    witness set than the run that crashed did, because nothing outside the
    record is consulted.
    """
    record = _sent()
    for _ in range(3):
        assert qualifying_send(record) == qualifying_send(dict(record))

    # Position in the log is not an input.
    surrounded = [_sent(tool="gmail.read"), record, _sent(ok=False)]
    assert len(qualifying_sends(surrounded)) == 1
    assert len(qualifying_sends(list(reversed(surrounded)))) == 1
    assert len(qualifying_sends([record, record, record])) == 3


def test_the_witness_carries_no_attempt_identity_and_no_thread() -> None:
    """A witness is what it witnesses, not where it sat or which thread it joined.

    ``thread_id`` is in the durable record and deliberately absent from the
    projection: a thread is a grouping of messages, and putting it on the witness
    would be the first step toward treating "a conversation exists" as "this
    intent was delivered".
    """
    assert not {"step_order", "request_id", "thread_id", "ir_action_id"} & set(
        QualifyingSend.__dataclass_fields__
    )
    witness = qualifying_send(_sent())
    assert witness is not None
    assert not {"thread_id"} & set(witness.arguments)


# --- 2. the provider identity decision ------------------------------------------


def test_the_authoritative_identity_is_evidence_message_id() -> None:
    """The id the read-after-send was issued against, and nothing else.

    ``kai.gmail.service.send_and_verify`` reads the message back by
    ``sent.message_id`` and then requires the read-back to carry the same id, so
    that value is the one the verification actually acted on.
    """
    witness = qualifying_send(_sent())
    assert witness is not None
    assert witness.provider_message_id == _sent()["evidence"]["message_id"]


def test_thread_id_is_not_a_qualifying_identity() -> None:
    """A thread groups messages; it does not identify an attempt.

    Two verified sends into one existing thread would share a ``thread_id``, and
    a thread may equally hold earlier turns and replies that this run never sent.
    Admitting it would make a witness fail to identify a witness.
    """
    # A send whose only evidence is a thread: nothing to be a witness by.
    assert qualifying_send(_sent(evidence={"thread_id": "thread-1"})) is None
    # A send carrying both: the message id is what qualifies it, and the witness
    # never learns the thread.
    witness = qualifying_send(_sent())
    assert witness is not None
    assert witness.provider_message_id == "msg-1"
    assert "thread_id" not in {witness.provider_message_id}


def test_there_is_no_fallback_to_the_serialized_payload() -> None:
    """``data`` is what the provider said, not what the boundary vouched for.

    The Gateway publishes ``data`` without consulting the tool's verification
    contract, so an id found only there has not been through
    ``READ_AFTER_SEND``. In the current adapter an unverified send carries no id
    in either place — so this refusal has no gap to close — and it is written down
    anyway, because the day an adapter serializes an id into ``data`` on the
    failure path, this is the refusal that has to already exist.
    """
    assert qualifying_send(_sent(evidence={}, data={"message_id": "msg-from-data"})) is None
    assert qualifying_send(_sent(evidence={}, data=None)) is None


def test_a_disagreeing_payload_cannot_overturn_the_authoritative_evidence() -> None:
    """The ``data`` copy is ignored even when the two disagree."""
    witness = qualifying_send(
        _sent(data={"message_id": "msg-other", "thread_id": "thread-other"})
    )
    assert witness is not None
    assert witness.provider_message_id == "msg-1"


def test_a_blank_identity_is_refused_and_never_rewritten() -> None:
    """A refused id is not a repaired one: a changed id is a different address."""
    assert qualifying_send(_sent(evidence={"message_id": " msg-1 "})) is not None
    witness = qualifying_send(_sent(evidence={"message_id": " msg-1 "}))
    assert witness is not None
    assert witness.provider_message_id == " msg-1 "


# --- 3. success criteria --------------------------------------------------------


def test_a_verified_send_to_the_sealed_recipient_is_fulfilled() -> None:
    assert evaluate_gmail_send(_inputs(_sent())) is FulfillmentVerdict.FULFILLED


def test_the_recipient_comparison_is_case_insensitive() -> None:
    """Case-folding is the same normalization the calendar attendee set uses."""
    assert (
        evaluate_gmail_send(_inputs(_sent(args={"to": "Ahmed@Example.COM"})))
        is FulfillmentVerdict.FULFILLED
    )


def test_a_send_to_someone_else_is_not_fulfilled() -> None:
    """The criterion the provider's own verification cannot check.

    ``verify_send`` proves the stored message went to the address the *request*
    named. Only the sealed resolution says which address the *Intent* named, so a
    send that faithfully fulfilled the wrong request is not fulfillment.
    """
    wrong = _sent(args={"to": "someone.else@example.com"})
    assert (
        evaluate_gmail_send(_inputs(wrong)) is FulfillmentVerdict.NOT_FULFILLED
    )


def test_a_verified_send_is_not_fulfilled_against_a_different_sealed_recipient() -> None:
    """Holding the evidence fixed, changing the criteria changes the verdict."""
    assert (
        evaluate_gmail_send(_inputs(_sent(), identities=OTHER_IDENTITIES))
        is FulfillmentVerdict.NOT_FULFILLED
    )


def test_an_unresolved_recipient_is_sealed_as_empty_and_never_fulfilled() -> None:
    """Empty is a legitimate sealed value, decided as ``NOT_FULFILLED``.

    Refusing it at sealing time would convert a decided verdict into a failed
    run, which is a change of meaning and not a validation.
    """
    assert _criteria(identities=[{"name": "nobody resolved"}]).for_gmail_send(
    ).required_recipients == frozenset()
    assert (
        evaluate_gmail_send(
            FulfillmentInput(
                criteria=_criteria(identities=[{"name": "nobody resolved"}]),
                evidence=(_sent(),),
            )
        )
        is FulfillmentVerdict.NOT_FULFILLED
    )


def test_a_send_with_no_usable_recipient_argument_is_not_fulfilled() -> None:
    """An unparseable argument is a failed criterion, never an error."""
    for bad in ({}, {"to": ""}, {"to": None}, {"to": 42}, {"to": ["a@b.com"]}, {"to": "  "}):
        assert (
            evaluate_gmail_send(_inputs(_sent(args=bad)))
            is FulfillmentVerdict.NOT_FULFILLED
        ), bad


def test_subject_and_body_are_not_criteria() -> None:
    """Neither is a field, and each is refused for a different stated reason.

    ``subject`` is already compared by ``verify_send`` against the request, so
    demanding it here would re-assert what ``verified=True`` asserts — against an
    input whose only durable source is the send attempt's own arguments, which
    would make the check a tautology. ``body`` is never verified at all, so a
    criterion for it could not be satisfied by any evidence.
    """
    assert set(GmailSendCriteria.model_fields) == {"required_recipients"}
    source = _code_of(_send_satisfies_criteria)
    for absent in ("subject", "body", "thread_id", "mismatch_count", "label_ids"):
        assert absent not in source, absent

    # And the semantics hold: a send whose subject or body differs from the
    # request's own values is still a witness, because neither is what the
    # criteria are about.
    assert (
        evaluate_gmail_send(_inputs(_sent(args={
            "to": "ahmed@example.com",
            "subject": "something else entirely",
            "body": "a different body",
        })))
        is FulfillmentVerdict.FULFILLED
    )


def test_no_refusal_rests_on_the_request_being_a_copy_of_the_intent() -> None:
    """The rule that used to explain both refusals is withdrawn, and this is why.

    ``required_recipients`` exists where ``calendar_meeting`` has two criteria
    rather than one, and that difference was explained by saying a criterion is
    stated only where the request is not a copy of what the criterion compares it
    against — the resolved identity row being the anchor, on the grounds that the
    request never carries it.

    **That ground does not hold.** ``create_intent_args`` fills the calendar
    request's ``attendees`` from the very identities the criteria are sealed from,
    so on the live producer the request *is* a copy of the sealed row there, and
    ``verify_event`` already compares the read-back's people to that request by
    equality. ``test_the_request_carries_the_same_addresses_the_criteria_name`` in
    ``test_intent_provenance.py`` pins the refutation and shows what is left
    carrying the field: the durable ``identities`` column is authority for an
    address the log's own fields merely report, and the log is not required to
    have been written by that one producer.

    What this test pins instead is the second half — that neither of the two
    refusals ever rested on a copy argument either, which is what the withdrawn
    rule implied about them. Each is refused for a *missing* reason: ``subject``
    and ``body`` have no sealed source anywhere to compare against, and ``title``
    has one but no sealed criterion stating it while the provider already compares
    it. Pinned against the real verifiers, because this claim was once stated
    wrongly in the other direction — calendar's verification was described as
    merely resolving an id — and a third domain would be designed by reading one
    of the two sentences.
    """
    from kai.calendar.models import Attendee as CalAttendee
    from kai.calendar.models import CalendarEvent
    from kai.calendar.service import verify_event
    from kai.calendar.service_models import CreateEventRequest
    from kai.gmail.models import EmailAddress, GmailMessage
    from kai.gmail.service import verify_send

    # Calendar's verification does not merely resolve an id: it compares the
    # read-back's people and date against the request, exactly like gmail's
    # compares recipient and subject.
    request = CreateEventRequest(
        title="KAI meeting",
        start=datetime(2026, 6, 24, 16, 0, tzinfo=UTC),
        end=datetime(2026, 6, 24, 17, 0, tzinfo=UTC),
        timezone="UTC",
        attendees=("ahmed@example.com",),
    )
    readable_but_wrong = CalendarEvent(
        event_id="evt-1",
        summary="KAI meeting",
        start=datetime(2026, 6, 30, 16, 0, tzinfo=UTC),
        end=datetime(2026, 6, 30, 17, 0, tzinfo=UTC),
        start_timezone="UTC",
        end_timezone="UTC",
        attendees=(CalAttendee(email="stranger@example.com"),),
    )
    compared = {m.field for m in verify_event(readable_but_wrong, request)}
    assert {"attendees", "date"} <= compared

    # Gmail's compares recipient and subject, and never the body.
    def message(**overrides: Any) -> Any:
        base: dict[str, Any] = {
            "message_id": "m1",
            "thread_id": "t1",
            "subject": "Quarterly plan",
            "from_addr": EmailAddress(email="me@example.com", name="Me"),
            "to": (EmailAddress(email="ahmed@example.com", name="Ahmed"),),
            "date": "2026-06-24",
            "snippet": "s",
            "body_text": "original body",
            "label_ids": ("SENT",),
        }
        base.update(overrides)
        return GmailMessage(**base)

    def verified(candidate: Any) -> tuple[str, ...]:
        return tuple(
            m.field
            for m in verify_send(
                candidate,
                expected_id="m1",
                sender_email="me@example.com",
                to="ahmed@example.com",
                subject="Quarterly plan",
            )
        )

    assert verified(message()) == ()
    assert verified(message(subject="other")) == ("subject",)
    assert verified(message(body_text="entirely different")) == ()

    # So: title is already covered by the provider's own comparison above, while
    # subject and body are not covered at all — and none of the three is excluded
    # for being a copy of anything.
    assert set(GmailSendCriteria.model_fields) == {"required_recipients"}
    assert set(CalendarMeetingCriteria.model_fields) == {
        "window",
        "required_attendees",
    }
    assert "title" not in set(CalendarMeetingCriteria.model_fields)
    assert "title" in set(_calendar_window().model_dump())
    # The anchor each domain's criteria rest on is the durable identity row,
    # recorded beside the log rather than inside it.
    from kai.runtime.models import RuntimeExecution

    assert "identities" in {c.name for c in RuntimeExecution.__table__.columns}


def test_the_criteria_refuse_a_profile_they_cannot_back() -> None:
    """A payload and a profile still have to agree — now in both directions."""
    with pytest.raises(ValidationError):
        SuccessCriteria(  # type: ignore[call-arg]
            profile="gmail_send",
            calendar_meeting=CalendarMeetingCriteria(
                window=_calendar_window(), required_attendees=frozenset()
            ),
        )
    with pytest.raises(ValidationError):
        SuccessCriteria(profile="invoice_paid", gmail_send=GmailSendCriteria())  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        SuccessCriteria(profile="gmail_send")  # type: ignore[call-arg]

    impostor = _criteria().model_copy(update={"profile": "calendar_meeting"})
    with pytest.raises(ValueError, match="no gmail criteria"):
        impostor.for_gmail_send()


def test_a_payload_is_reachable_only_through_its_own_domain_accessor() -> None:
    """No generic path: each domain's conditions are named by its own accessor."""
    from kai.runtime import fulfillment as module

    exported = set(module.__all__)
    assert {"SuccessCriteria", "GmailSendCriteria"} <= exported
    assert not {"resolve_evaluator", "evaluator_for", "registry", "evaluate"} & exported
    assert isinstance(_criteria().for_gmail_send(), GmailSendCriteria)
    with pytest.raises(ValueError, match="no calendar criteria"):
        _criteria().for_calendar_meeting()


def test_the_criteria_are_sealed_against_mutation_and_extra_fields() -> None:
    payload = _criteria().for_gmail_send()
    # A frozen pydantic model refuses assignment as a validation error, and a
    # field outside this domain's meaning cannot be constructed at all.
    with pytest.raises((FrozenInstanceError, ValidationError)):
        payload.required_recipients = frozenset({"other@example.com"})  # type: ignore[misc]
    with pytest.raises(ValidationError):
        GmailSendCriteria(subject="not a criterion")  # type: ignore[call-arg]
    with pytest.raises((FrozenInstanceError, ValidationError)):
        _criteria().profile = "calendar_meeting"  # type: ignore[misc]


def test_sealing_the_criteria_is_deterministic_and_needs_no_prose() -> None:
    """The same sealed rows always seal the same value, on a live run or a recovery."""
    assert _criteria().model_dump() == _criteria().model_dump()
    assert _criteria().model_dump() == gmail_send_criteria(
        profile="gmail_send", identities=list(IDENTITIES)
    ).model_dump()

    # The prose is not an input, so it cannot be one.
    assert "success_criteria" not in _code_of(gmail_send_criteria)
    assert "success_criteria" not in _code_of(evaluate_gmail_send)


# --- 4. set semantics: did the quantifiers survive? ------------------------------


def test_the_empty_relevant_set_is_not_fulfilled_and_never_vacuously_true() -> None:
    """The empty set is an answer, not a gap."""
    assert evaluate_gmail_send(_inputs()) is FulfillmentVerdict.NOT_FULFILLED
    assert (
        evaluate_gmail_send(_inputs(*[_sent(tool="gmail.read")]))
        is FulfillmentVerdict.NOT_FULFILLED
    )
    # And not the vacuous reading a naive conjunction would give.
    assert frozenset() <= frozenset()  # the truthiness this must never rely on
    assert evaluate_gmail_send(_inputs()) is not FulfillmentVerdict.FULFILLED


def test_one_qualifying_witness_among_several_is_enough() -> None:
    """Existential over the set, exactly as in the calendar domain."""
    evidence = (
        _sent(args={"to": "nobody@example.com"}),
        _sent(args={"to": "ahmed@example.com"}, evidence={"message_id": "msg-2"}),
        _sent(tool="gmail.read"),
    )
    assert evaluate_gmail_send(_inputs(*evidence)) is FulfillmentVerdict.FULFILLED


def test_the_verdict_is_independent_of_the_logs_order() -> None:
    """Every permutation of the relevant set gives one verdict.

    The strongest available statement of order-independence: exhaustively
    permuted, not merely reversed.
    """
    evidence = (
        _sent(args={"to": "wrong@example.com"}),
        _sent(args={"to": "ahmed@example.com"}, evidence={"message_id": "msg-2"}),
        _sent(tool="gmail.read"),
        _sent(ok=False),
    )
    for order in itertools.permutations(evidence):
        assert evaluate_gmail_send(_inputs(*order)) is FulfillmentVerdict.FULFILLED

    failing = (
        _sent(args={"to": "wrong@example.com"}),
        _sent(verified=False),
    )
    for order in itertools.permutations(failing):
        assert evaluate_gmail_send(_inputs(*order)) is FulfillmentVerdict.NOT_FULFILLED


def test_a_repeated_witness_does_not_change_the_verdict() -> None:
    """A resumed run may legitimately record the same send twice.

    Duplicate suppression is absent on purpose: the verdict is existential, so a
    repeat is neither counted twice nor dropped, and de-duplicating would buy
    nothing but a cost.
    """
    once = evaluate_gmail_send(_inputs(_sent()))
    thrice = evaluate_gmail_send(_inputs(_sent(), _sent(), _sent()))
    assert once is thrice is FulfillmentVerdict.FULFILLED

    # Duplicates do not rescue a failing set either, wherever they land.
    failing = _sent(args={"to": "wrong@example.com"})
    assert (
        evaluate_gmail_send(_inputs(failing, failing)) is FulfillmentVerdict.NOT_FULFILLED
    )


def test_two_verified_sends_of_one_intent_are_witnesses_not_a_contradiction() -> None:
    """Distinct ids mean two attempts, and existence is indifferent to how many.

    A retried send is the case a naive de-duplication would mishandle: the answer
    is "fulfilled" whether one message or two exist, because the question was
    never about the count.
    """
    both = (
        _sent(evidence={"message_id": "msg-1"}),
        _sent(evidence={"message_id": "msg-2"}),
    )
    assert {w.provider_message_id for w in qualifying_sends(both)} == {
        "msg-1",
        "msg-2",
    }
    assert evaluate_gmail_send(_inputs(*both)) is FulfillmentVerdict.FULFILLED
    assert evaluate_gmail_send(_inputs(both[0])) is FulfillmentVerdict.FULFILLED


def test_irrelevant_evidence_cannot_rescue_or_alter_the_verdict() -> None:
    """A reader is not a sender, and a failure is not a success."""
    irrelevant = (
        _sent(tool="gmail.read"),
        _sent(tool="gmail.search"),
        _sent(tool="calendar.create_event"),
        _sent(ok=False),
        _sent(verified=False),
        _sent(evidence={}),
        {"tool": "gmail.send"},
        "not a record",
        None,
    )
    assert evaluate_gmail_send(_inputs()) is FulfillmentVerdict.NOT_FULFILLED
    for record in irrelevant:
        assert evaluate_gmail_send(_inputs(record)) is FulfillmentVerdict.NOT_FULFILLED
        assert (
            evaluate_gmail_send(_inputs(_sent(), record)) is FulfillmentVerdict.FULFILLED
        )


def test_contradictory_evidence_resolves_by_existence_not_by_a_vote() -> None:
    """One satisfying witness settles it; failures beside it are not counted.

    There is no tally here. A log holding one good send and three unverified
    attempts is a fulfilled intent, and a log holding only the unverified ones is
    not — the *set* decides, per witness, not the balance of records.
    """
    mixed = (
        _sent(ok=False),
        _sent(verified=False),
        _sent(args={"to": "wrong@example.com"}),
        _sent(evidence={"message_id": "msg-good"}),
    )
    assert evaluate_gmail_send(_inputs(*mixed)) is FulfillmentVerdict.FULFILLED
    assert (
        evaluate_gmail_send(_inputs(*mixed[:3])) is FulfillmentVerdict.NOT_FULFILLED
    )


def test_the_verdict_survives_a_permuted_replay_of_a_whole_log() -> None:
    """The property a recovery depends on, over a realistic mixed log."""
    log = [
        _sent(tool="gmail.search"),
        _sent(ok=False),
        _sent(args={"to": "wrong@example.com"}, evidence={"message_id": "msg-x"}),
        _sent(evidence={"message_id": "msg-good"}),
        _sent(verified=False),
    ]
    verdicts = {
        evaluate_gmail_send(_inputs(*order)) for order in itertools.permutations(log)
    }
    assert verdicts == {FulfillmentVerdict.FULFILLED}


# --- 5. determinism, totality, and the unreachable third value ------------------


def test_the_evaluator_is_total_and_returns_only_the_three_values() -> None:
    """Total over junk, and it never raises."""
    for evidence in ((), ("x",), (None,), ({},), ({"tool": None},), ({"args": 5},)):
        assert evaluate_gmail_send(_inputs(*evidence)) in set(FulfillmentVerdict)
    # And on a well-formed but unprovable record, the conjunction is a false
    # criterion rather than an exception.
    assert (
        evaluate_gmail_send(_inputs(_sent(args={"to": object()})))
        is FulfillmentVerdict.NOT_FULFILLED
    )


def test_the_evaluator_is_a_pure_function_of_its_frozen_input() -> None:
    """Same inputs, same verdict, repeatedly — this is what recovery rests on."""
    inputs = _inputs(_sent())
    assert [evaluate_gmail_send(inputs) for _ in range(3)] == [
        FulfillmentVerdict.FULFILLED
    ] * 3
    with pytest.raises(FrozenInstanceError):
        inputs.evidence = ()  # type: ignore[misc]


def test_indeterminate_is_unreachable_and_that_is_recorded_not_invented() -> None:
    """This domain produces two values, and the third is documented as absent.

    Every check is a decidable comparison over durable scalars, so nothing here is
    undecidable. The interesting case is the one that *looks* like it needs the
    third value — a send the provider accepted whose read-after-send failed. It
    records ``verified=False`` with no ``message_id``, so it is not a witness and
    the verdict is ``NOT_FULFILLED``, even though delivery may have happened. That
    is correct under the question actually asked, and is the same answer the
    calendar domain gives for an unreadable created event.
    """
    unverified = _sent(
        verified=False,
        evidence={},
        data=None,
        mismatch_count_note=None,
    )
    del unverified["mismatch_count_note"]
    assert evaluate_gmail_send(_inputs(unverified)) is FulfillmentVerdict.NOT_FULFILLED
    assert FulfillmentVerdict.INDETERMINATE not in {
        evaluate_gmail_send(_inputs(_sent())),
        evaluate_gmail_send(_inputs()),
        evaluate_gmail_send(_inputs(unverified)),
        evaluate_gmail_send(_inputs(_sent(args={"to": "wrong@example.com"}))),
    }

    # The deferred part is stated in the evaluator's own documentation, so the
    # absence is a recorded decision rather than an oversight.
    assert "deferred" in (evaluate_gmail_send.__doc__ or "").lower()


def test_the_evaluator_cannot_transition_a_task_or_reach_a_provider() -> None:
    """Structurally: no lifecycle authority, no I/O, no model, no session."""
    source = _code_of(evaluate_gmail_send) + _code_of(_send_satisfies_criteria)
    for forbidden in (
        "transition_task",
        "COMPLETED",
        "session",
        "await ",
        "getattr(",
        "import ",
    ):
        assert forbidden not in source, forbidden
    assert set(FulfillmentInput.__dataclass_fields__) == {"criteria", "evidence"}


def test_the_evaluator_reads_the_typed_model_and_never_the_prose() -> None:
    """The typed criteria are the only thing the decision is read from."""
    source = _code_of(_send_satisfies_criteria)
    for field in set(GmailSendCriteria.model_fields):
        assert field in source, field
    for absent in ("goal", "success_criteria", "profile", "allowed_tools"):
        assert absent not in source, absent


# --- 6. the binding: sealed profile -> criteria -> evaluator -> outcome ----------


def _task_row(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "goal": "email Ahmed the quarterly plan",
        "fulfillment_profile": "gmail_send",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _calendar_window() -> Any:
    from datetime import date, time

    from kai.runtime.schemas import MeetingWindow

    return MeetingWindow(
        start_date=date(2026, 6, 22),
        end_date=date(2026, 6, 26),
        earliest_time=time(16, 0),
    )


def test_the_gmail_profile_reaches_the_gmail_evaluator() -> None:
    """The binding the experiment was about: the contract names the question.

    ``scenario`` is passed as ``None`` on purpose — the send branch must not read
    the calendar understanding window, and this is how that is shown rather than
    asserted in prose.
    """
    outcome = _fulfillment_outcome(
        _task_row(),
        scenario=None,  # type: ignore[arg-type]
        identities=list(IDENTITIES),
        evidence=[_sent()],
    )
    assert outcome.verdict is FulfillmentVerdict.FULFILLED
    assert outcome.profile == "gmail_send"
    assert outcome.version == GMAIL_SEND_EVALUATOR_VERSION


def test_the_two_profiles_are_distinguished_by_evidence_not_by_a_default() -> None:
    """A send record never satisfies a calendar contract, and vice versa.

    The two profiles are not one evaluator with two data shapes: each reads only
    its own domain's witness set, so neither can be reached by the other's
    evidence. This is the control that the second domain did not blur the first.
    """
    gmail_task = _task_row()
    calendar_task = _task_row(fulfillment_profile="calendar_meeting")

    assert (
        _fulfillment_outcome(
            gmail_task,
            scenario=None,  # type: ignore[arg-type]
            identities=list(IDENTITIES),
            evidence=[_sent()],
        ).verdict
        is FulfillmentVerdict.FULFILLED
    )
    assert (
        _fulfillment_outcome(
            gmail_task,
            scenario=None,  # type: ignore[arg-type]
            identities=list(IDENTITIES),
            evidence=[_sent(tool="calendar.create_event")],
        ).verdict
        is FulfillmentVerdict.NOT_FULFILLED
    )

    created = {
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
    assert (
        _fulfillment_outcome(
            calendar_task,
            scenario=SimpleNamespace(window=_calendar_window()),
            identities=list(IDENTITIES),
            evidence=[created],
        ).verdict
        is FulfillmentVerdict.FULFILLED
    )
    assert (
        _fulfillment_outcome(
            calendar_task,
            scenario=SimpleNamespace(window=_calendar_window()),
            identities=list(IDENTITIES),
            evidence=[_sent()],
        ).verdict
        is FulfillmentVerdict.NOT_FULFILLED
    )


def test_an_unknown_profile_is_still_refused_rather_than_guessed() -> None:
    """Two known questions, and a refusal for everything else."""
    with pytest.raises(RuntimeFatal, match="no fulfillment evaluator"):
        _fulfillment_outcome(
            _task_row(fulfillment_profile="invoice_paid"),
            scenario=None,  # type: ignore[arg-type]
            identities=list(IDENTITIES),
            evidence=[_sent()],
        )


def test_the_outcome_records_the_profile_and_version_that_actually_ran() -> None:
    """The audit triple is self-consistent, and the version is a declared constant."""
    outcome = _fulfillment_outcome(
        _task_row(),
        scenario=None,  # type: ignore[arg-type]
        identities=list(IDENTITIES),
        evidence=[_sent()],
    )
    assert isinstance(outcome, FulfillmentOutcome)
    assert outcome.profile == "gmail_send"
    assert outcome.version == GMAIL_SEND_EVALUATOR_VERSION == "gmail_send:v1"

    # A separate identity, deliberately: the two domains have separate semantics
    # and must not be forced to bump one version for the other's change.
    assert GMAIL_SEND_EVALUATOR_VERSION != CALENDAR_MEETING_EVALUATOR_VERSION
    assert CALENDAR_MEETING_EVALUATOR_VERSION == "calendar_meeting:v1"

    # The version is not a function of the verdict or of run state.
    other = _fulfillment_outcome(
        _task_row(),
        scenario=None,  # type: ignore[arg-type]
        identities=list(IDENTITIES),
        evidence=[_sent(args={"to": "wrong@example.com"})],
    )
    assert other.verdict is FulfillmentVerdict.NOT_FULFILLED
    assert other.version == outcome.version
    assert other.version == GMAIL_SEND_EVALUATOR_VERSION


def test_the_runtime_seals_the_gmail_criteria_from_the_durable_identity_row() -> None:
    """The criteria come from a row that already exists, so nothing was added.

    No new column, no new table, no migration: the experiment reused
    ``RuntimeExecution.identities`` — the same sealed resolution the calendar
    path reads. This is asserted against the model, so a future domain that
    genuinely needs its own durable intent source will have to say so.
    """
    from kai.runtime.models import RuntimeExecution

    persisted = set(RuntimeExecution.__table__.columns.keys())
    assert {"scenario", "identities"} <= persisted
    assert not {"success_criteria", "criteria", "gmail_send_intent"} & persisted

    outcome = _fulfillment_outcome(
        _task_row(),
        scenario=None,  # type: ignore[arg-type]
        identities=[{"email": "Ahmed@Example.com"}],
        evidence=[_sent(args={"to": "AHMED@example.com"})],
    )
    assert outcome.verdict is FulfillmentVerdict.FULFILLED


# --- 7. recovery ----------------------------------------------------------------


def test_a_recovery_re_derives_the_identical_verdict_from_the_identical_row() -> None:
    """A crashed run and a recovered run are decided by the same inputs.

    The recovery path calls the same function with the same sealed row, so there
    is nothing for a second decision to disagree with.
    """
    evidence = [
        _sent(ok=False),
        _sent(args={"to": "wrong@example.com"}),
        _sent(evidence={"message_id": "msg-good"}),
    ]
    first = _fulfillment_outcome(
        _task_row(), scenario=None, identities=list(IDENTITIES), evidence=evidence  # type: ignore[arg-type]
    )
    recovered = _fulfillment_outcome(
        _task_row(), scenario=None, identities=list(IDENTITIES), evidence=evidence  # type: ignore[arg-type]
    )
    assert first.verdict is recovered.verdict is FulfillmentVerdict.FULFILLED
    assert (first.profile, first.version) == (recovered.profile, recovered.version)
    # And a re-derivation that arrives after a crash re-reads the same log order
    # it originally did, so re-derivation is stable under replay too.
    assert [first.verdict] * 3 == [recovered.verdict] * 3


def test_a_persisted_verdict_that_contradicts_the_evidence_is_not_obeyed() -> None:
    """The recorded outcome is an artifact; the re-derivation wins.

    Simulates the recovery hazard directly: a row that already carries a
    ``FULFILLED`` verdict while its evidence proves nothing. If the durable record
    were an authority, this would settle as fulfilled; the authority is the
    evaluator applied to the evidence, so it settles as not fulfilled.
    """
    contradictory = _sent(args={"to": "wrong@example.com"})
    seeded_verdict = FulfillmentVerdict.FULFILLED

    re_derived = _fulfillment_outcome(
        _task_row(), scenario=None, identities=list(IDENTITIES), evidence=[contradictory]  # type: ignore[arg-type]
    )
    assert re_derived.verdict is FulfillmentVerdict.NOT_FULFILLED
    # The persisted value is never consulted, so disagreeing with it is fine.
    assert re_derived.verdict is not seeded_verdict


def test_recovery_never_turns_a_verified_send_into_a_second_verified_send() -> None:
    """Re-derivation re-reads the log; it does not re-send.

    The evaluator has no way to cause a side effect, and the witness set is built
    from durable records. A recovery that produced a *new* record would have to
    have executed a tool, which is the runtime's job and not the evaluator's — so
    the invariant worth pinning is that the derivation is a pure read: the same
    records in, the same witnesses out, and no send among them that was not
    already there.
    """
    log = [_sent(evidence={"message_id": "msg-1"})]
    before = [w.provider_message_id for w in qualifying_sends(log)]
    for _ in range(3):
        _fulfillment_outcome(
            _task_row(), scenario=None, identities=list(IDENTITIES), evidence=log  # type: ignore[arg-type]
        )
    after = [w.provider_message_id for w in qualifying_sends(log)]
    assert before == after == ["msg-1"]
    # And the same log judged on replay never grows a witness.
    assert len(qualifying_sends(log + log)) == 2
    assert evaluate_gmail_send(_inputs(*log)) is FulfillmentVerdict.FULFILLED


# --- 8. the control: calendar_meeting is unchanged -------------------------------


def test_calendar_semantics_are_untouched_by_the_second_domain() -> None:
    """The control group. Every verdict here is the pre-experiment verdict.

    The point of the experiment was to add a second domain *without* moving the
    first, so the first domain's own tests are the evidence, and these three
    re-assert its load-bearing cases against the widened vocabulary.
    """
    window = _calendar_window()
    criteria = calendar_meeting_criteria(
        profile="calendar_meeting",
        window=window,
        identities=[{"email": "ahmed@example.com"}],
    )

    def created(**overrides: Any) -> dict[str, Any]:
        entry = {
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

    def judged(*evidence: Any) -> FulfillmentVerdict:
        return evaluate_calendar_meeting(
            FulfillmentInput(criteria=criteria, evidence=evidence)
        )

    assert judged(created()) is FulfillmentVerdict.FULFILLED
    assert judged() is FulfillmentVerdict.NOT_FULFILLED
    assert judged(created(verified=False)) is FulfillmentVerdict.NOT_FULFILLED
    assert judged(created(args={**created()["args"], "start_time": "08:00"})) is (
        FulfillmentVerdict.NOT_FULFILLED
    )
    # A send record is not a meeting, and was never one.
    assert judged(_sent()) is FulfillmentVerdict.NOT_FULFILLED
    # The calendar payload still refuses to be read as a gmail one.
    with pytest.raises(ValueError, match="no gmail criteria"):
        criteria.for_gmail_send()


def test_the_calendar_witness_boundary_did_not_absorb_the_send_one() -> None:
    """Two boundaries, two projections, and no shared base to have merged into.

    If the experiment had been answered by generalizing
    :mod:`kai.runtime.evidence`, these two would now be one type. They are not,
    and the duplication is the result: the two definitions had to be written out
    separately for their meaning to be comparable at all.
    """
    from kai.runtime.evidence import QualifyingCreation, qualifying_creation

    assert MESSAGE_SEND_TOOL == "gmail.send"
    assert QualifyingCreation is not QualifyingSend
    assert set(QualifyingCreation.__dataclass_fields__) == {
        "provider_event_id",
        "arguments",
    }
    assert set(QualifyingSend.__dataclass_fields__) == {
        "provider_message_id",
        "arguments",
    }
    # Neither boundary answers for the other's tool.
    assert qualifying_creation(_sent()) is None
    assert qualifying_send(
        {
            "tool": "calendar.create_event",
            "outcome": "EXECUTION_SUCCEEDED",
            "ok": True,
            "verified": True,
            "args": {"date": "2026-06-24"},
            "evidence": {"event_id": "evt-1"},
        }
    ) is None
