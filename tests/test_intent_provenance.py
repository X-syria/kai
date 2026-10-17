"""Where each SuccessCriteria value actually comes from — provenance, not shape.

The two fulfillment test files pin *what* each domain decides. This one asks the
question underneath that, per value rather than per function:

```text
Sealed Intent / Contract
        -> Intent-bearing fields
        -> Domain projection
        -> Typed SuccessCriteria
        -> Deterministic Evaluator
```

For every field of every criteria payload: which durable row it is read from,
what transformation it passes through, which of the four things it is (intent
content, contract statement, provider/identity output, verification support),
and what settles it.

Three results are pinned here that the earlier rounds asserted in prose and that
direct inspection of the code does not support, so they are pinned as *what the
code does say* rather than as what the prose hoped:

1. **The sealed contract contributes the profile and nothing else.** ``goal`` and
   the prose ``success_criteria`` are not inputs to any criteria builder, so the
   criteria are a projection of the sealed *understanding* rows
   (``RuntimeExecution.scenario`` / ``.identities``), not of the contract's own
   statement of what the user asked.
2. **``row.identities`` is a join, not an intent artifact.** Its ``name`` half is
   the model's reading of the goal; its ``email`` half is the identity resolver's
   output against live provider data; and the criteria read the address half
   only. So the criteria are keyed on *resolved addresses* and are blind to guest
   names, to multiplicity of names, and to how an identity was established.
3. **The previously stated reason the address anchor is a criterion is refuted.**
   The calendar request does carry the sealed addresses —
   ``create_intent_args(attendee_emails=identities...)`` — so "the request does
   not carry the resolved identity row" is false here. On the single live producer
   the anchor is in fact *implied* by witness qualification. What remains
   load-bearing on the live path is the range, and that is pinned too.

No database, no HTTP, no model: the whole chain under test is a pure function of
durable rows plus the durable log.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from typing import Any, Final, get_args

from kai.calendar.models import Attendee as CalAttendee
from kai.calendar.models import CalendarEvent
from kai.calendar.service import verify_event
from kai.calendar.service_models import CreateEventRequest
from kai.identity.models import IdentityStatus
from kai.runtime.fulfillment import (
    CalendarMeetingCriteria,
    FulfillmentInput,
    FulfillmentVerdict,
    GmailSendCriteria,
    SuccessCriteria,
    calendar_meeting_criteria,
    evaluate_calendar_meeting,
    evaluate_gmail_send,
    gmail_send_criteria,
)
from kai.runtime.models import RuntimeExecution
from kai.runtime.schemas import (
    MeetingWindow,
    RuntimeIdentity,
    UnderstandingOutput,
    redact_identities,
)
from kai.runtime.service import (
    _POSITIVE_IDENTITY,
    _fulfillment_outcome,
    _read_slot_items,
)
from kai.runtime.translator import (
    SLICE_FULFILLMENT_PROFILE,
    SLICE_SUCCESS_CRITERIA,
    build_slice_contract,
    create_intent_args,
    pick_slot,
)
from kai.runtime.understanding import UNDERSTANDING_SYSTEM_PROMPT
from kai.tasks.schemas import TaskContractInput
from kai.tools.schemas import GmailSendArgs

WINDOW: Final = MeetingWindow(
    start_date=date(2026, 6, 22),
    end_date=date(2026, 6, 26),
    earliest_time=time(16, 0),
    title="KAI meeting",
)

#: One row as ``_resolve_guest_identities`` seals it: the model-supplied name, the
#: resolver's address, and the two fields that say how it was established.
IDENTITIES: Final = (
    {
        "name": "Ahmed",
        "email": "Ahmed@Example.com",
        "status": IdentityStatus.CONTACT_MATCH.value,
        "confidence": 0.8,
    },
)


def _code_of(function: Callable[..., Any]) -> str:
    """The function's source with the docstring stripped."""
    tree = ast.parse(inspect.getsource(function))
    node = tree.body[0]
    if (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    ):
        node.body.pop(0)
    return ast.unparse(node)


def _calendar_criteria(
    *, window: MeetingWindow = WINDOW, identities: Any = IDENTITIES
) -> SuccessCriteria:
    return calendar_meeting_criteria(
        profile="calendar_meeting", window=window, identities=identities
    )


def _gmail_criteria(identities: Any = IDENTITIES) -> SuccessCriteria:
    return gmail_send_criteria(profile="gmail_send", identities=identities)


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


def _sent(**overrides: Any) -> dict[str, Any]:
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
        "evidence": {"message_id": "msg-1", "label_ids": ["SENT"]},
    }
    entry.update(overrides)
    return entry


def _prepared(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "tool": "task.prepare",
        "outcome": "EXECUTION_SUCCEEDED",
        "ok": True,
        "verified": True,
        "args": {"objective": "prepare a trip"},
        "evidence": {"objective_provided": True, "step_count": 3, "missing_count": 3},
    }
    entry.update(overrides)
    return entry


def _task_row(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "goal": "schedule a meeting",
        "fulfillment_profile": "calendar_meeting",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _scenario() -> Any:
    return SimpleNamespace(window=WINDOW)


# --- 1. provenance: which durable rows the criteria are sealed from --------------


def test_each_criteria_names_the_sealed_rows_it_is_sealed_from() -> None:
    """The provenance is in the signature, so it cannot be widened silently.

    A builder that took one more input would change what the criteria are a
    projection of; asserting the parameter names pins that question where it is
    actually answered rather than in a comment.
    """
    assert set(inspect.signature(calendar_meeting_criteria).parameters) == {
        "profile",
        "window",
        "identities",
    }
    assert set(inspect.signature(gmail_send_criteria).parameters) == {
        "profile",
        "identities",
    }
    for builder in (calendar_meeting_criteria, gmail_send_criteria):
        source = _code_of(builder)
        for absent in (
            "evidence",
            "args",
            "data",
            "goal",
            "success_criteria",
            "session",
            "await ",
            "getattr(",
            "import ",
        ):
            assert absent not in source, (builder.__name__, absent)


def test_the_sealed_contract_supplies_the_profile_and_nothing_else() -> None:
    """Prose criteria and scope are not inputs to any criteria; the goal only to
    the one profile whose question is anchored to it.

    The prose being inert is proved elsewhere. For the calendar and gmail
    questions the goal is likewise not an input: the window those criteria are
    sealed from is the model's reading of the goal, committed in
    ``RuntimeExecution.scenario``, so two tasks whose sealed contracts disagree
    entirely about the goal and its success criteria are decided identically
    from the same understanding row.

    The ``task_preparation`` question is the deliberate exception: its sealed
    criterion is the digest of the persisted ``task.goal`` — the user intent
    fixed at submission — because a local preparation has no understanding row
    and no resolved identities to project from. So the goal is read there and
    only there, and a different goal is a different criterion by construction.
    """
    source = _code_of(_fulfillment_outcome)
    for absent in ("success_criteria", "allowed_tools", "risk_policy"):
        assert absent not in source, absent
    # ``task.goal`` is read once, inside the task_preparation branch and nowhere
    # else: the profile-gated read is visible here rather than buried in a call.
    assert source.count("task.goal") == 1
    assert source.index("if profile == _TASK_PREPARATION") < source.index("task.goal")

    baseline = _fulfillment_outcome(
        _task_row(),
        scenario=_scenario(),
        identities=list(IDENTITIES),
        evidence=[_created()],
    )
    other = _fulfillment_outcome(
        _task_row(
            goal="pay an invoice",
            success_criteria=["nothing about a meeting"],
            allowed_tools=["gmail.send"],
            risk_policy="CRITICAL",
        ),
        scenario=_scenario(),
        identities=list(IDENTITIES),
        evidence=[_created()],
    )
    assert baseline == other
    # The profile itself is the one field that does select the branch.
    gmail = _fulfillment_outcome(
        _task_row(fulfillment_profile="gmail_send"),
        scenario=None,
        identities=list(IDENTITIES),
        evidence=[_sent()],
    )
    assert gmail.profile == "gmail_send" and gmail != baseline
    # And on the preparation branch, the goal *is* the intent source: two goals
    # give two criteria, so the goal is not inert for that one question.
    prepared = _fulfillment_outcome(
        _task_row(fulfillment_profile="task_preparation", goal="prepare a trip"),
        scenario=None,
        identities=[],
        evidence=[_prepared()],
    )
    prepared_other = _fulfillment_outcome(
        _task_row(fulfillment_profile="task_preparation", goal="prepare a report"),
        scenario=None,
        identities=[],
        evidence=[_prepared()],
    )
    assert prepared.profile == "task_preparation"
    assert prepared != prepared_other


def test_provider_echo_moves_the_verdict_and_never_the_criteria() -> None:
    """Echo is what the evaluator reads; it is not what the criteria are.

    Sealing the criteria and judging the evidence are separate acts, so a change
    in anything the provider said — the serialized payload, the identity, the
    labels, the attempt ordinal — can move the verdict and cannot move the
    criteria. Both halves are asserted, because the first alone would be
    satisfied by a builder that ignored the log entirely for a trivial reason.
    """
    echoes: list[dict[str, Any]] = [
        _created(),
        _created(data={"event_id": "other", "htmlLink": "https://x"}),
        _created(evidence={"event_id": "evt-2", "mismatch_count": 0}),
        _created(step_order=99, request_id="req-zzz", ir_action_id="act-9"),
        _created(args={"date": "2026-06-30", "start_time": "17:30",
                       "attendees": ["ahmed@example.com"]}),
    ]
    criteria = {_calendar_criteria().model_dump_json()}
    assert len(criteria) == 1
    verdicts = {
        evaluate_calendar_meeting(
            FulfillmentInput(criteria=_calendar_criteria(), evidence=(echo,))
        )
        for echo in echoes
    }
    # The echo is load-bearing for the verdict, and the criteria were one value
    # throughout — so the two are provably not read from the same place.
    assert verdicts == {
        FulfillmentVerdict.FULFILLED,
        FulfillmentVerdict.NOT_FULFILLED,
    }

    sends: list[dict[str, Any]] = [
        _sent(),
        _sent(data={"message_id": "msg-2", "thread_id": "thread-9"}),
        _sent(evidence={"message_id": "msg-3", "label_ids": ["SENT", "DRAFT"]}),
        _sent(args={"to": "nobody@example.com"}),
    ]
    gmail_verdicts = {
        evaluate_gmail_send(
            FulfillmentInput(criteria=_gmail_criteria(), evidence=(send,))
        )
        for send in sends
    }
    assert gmail_verdicts == {
        FulfillmentVerdict.FULFILLED,
        FulfillmentVerdict.NOT_FULFILLED,
    }
    assert _gmail_criteria().model_dump_json() == _gmail_criteria().model_dump_json()


# --- 2. identity authority: what ``row.identities`` actually is ----------------


def test_the_criteria_are_keyed_on_the_address_and_not_on_the_guest_name() -> None:
    """The intent-bearing half of the identity row never reaches the criteria.

    Two consequences, both pinned because both are silent otherwise: a guest
    whose name the model rendered differently is the same criterion, and two
    distinct guests who resolved to one address collapse into one. The criteria
    state *which addresses must be present*, never *which people were asked for*
    — the row's ``name`` is used to look an address up and is then dropped.
    """
    renamed = _calendar_criteria(
        identities=[{**IDENTITIES[0], "name": "Ahmed Alkhatib"}]
    )
    assert renamed == _calendar_criteria()
    assert renamed.for_calendar_meeting().required_attendees == frozenset(
        {"ahmed@example.com"}
    )

    both = _calendar_criteria(
        identities=[
            {**IDENTITIES[0], "name": "Ahmed"},
            {**IDENTITIES[0], "name": "A. Alkhatib"},
        ]
    )
    assert both.for_calendar_meeting().required_attendees == frozenset(
        {"ahmed@example.com"}
    )
    # Same on the send side: the projection is identical, so the blindness is a
    # property of the anchor rather than of one domain's spelling of it.
    assert _gmail_criteria(
        identities=[
            {**IDENTITIES[0], "name": "Ahmed"},
            {**IDENTITIES[0], "name": "A. Alkhatib"},
        ]
    ).for_gmail_send().required_recipients == frozenset({"ahmed@example.com"})


def test_how_an_identity_was_established_is_not_part_of_the_criteria() -> None:
    """``status`` and ``confidence`` are a gate, not a success condition.

    The runtime refuses to run at all unless every guest resolved to a positive
    identity, so by the time a verdict is possible the establishing evidence has
    already done its work. Carrying it into the criteria would state a condition
    the evaluator could never see — and would let a lower-confidence resolution
    read as a weaker fulfillment, which is a different question.
    """
    assert frozenset(
        {
            IdentityStatus.CONTACT_MATCH,
            IdentityStatus.PROVIDER_VERIFIED,
            IdentityStatus.CHANNEL_VERIFIED,
        }
    ) == _POSITIVE_IDENTITY
    variants = [
        {**IDENTITIES[0], "status": status.value, "confidence": confidence}
        for status in sorted(_POSITIVE_IDENTITY, key=lambda s: s.value)
        for confidence in (0.0, 0.5, 1.0)
    ]
    assert {
        _calendar_criteria(identities=[variant]).model_dump_json()
        for variant in variants
    } == {_calendar_criteria().model_dump_json()}
    assert {
        _gmail_criteria(identities=[variant]).model_dump_json()
        for variant in variants
    } == {_gmail_criteria().model_dump_json()}


def test_the_identity_row_is_a_join_of_a_model_named_key_and_a_resolved_address() -> None:
    """``row.identities`` is neither intent nor execution identity on its own.

    It is the seam between them, and that is what makes it dangerous to call it
    either. The ``name`` is what the understanding stage read out of the goal; the
    ``email`` is what the identity resolver produced from live provider data or
    from a memory hint; and the criteria are sealed from the second while
    discarding the first. So the honest description of ``required_attendees`` is
    not "the guests the user asked for" but "the addresses our resolver produced
    for the guests the model named".
    """
    source = inspect.getsource(
        __import__("kai.runtime.service", fromlist=["AgentRuntime"]).AgentRuntime
    )
    # The row is built by walking the model's guest names, one resolution each.
    assert "for guest in scenario.guests:" in source
    assert "attendee identity not established" in source
    # And the row's own shape is the join: a name, an address, and two fields
    # describing the resolution. Nothing else is sealed beside them.
    assert set(RuntimeIdentity.model_fields) == {
        "name",
        "email",
        "status",
        "confidence",
    }
    published = redact_identities([dict(IDENTITIES[0])])
    assert [item.name for item in published] == ["Ahmed"]
    assert published[0].email == "A***@Example.com"
    # A row with no name or no status is not a row at all, which is what makes it
    # a resolution record rather than a free-form intent fragment.
    assert redact_identities([{"email": "ahmed@example.com"}]) == []


# --- 3. guests -> recipients: pinned as a structural assumption ----------------


def test_the_understanding_carries_no_recipient_intent_and_cannot_gain_one() -> None:
    """There is no field a recipient criterion could have been sealed from.

    This is the whole of the ``guests -> recipients`` question at the schema
    level, and it is why the equivalence cannot be established by reading the
    code: ``UnderstandingOutput`` is a meeting scenario — one intent literal, a
    non-empty list of display names, and a bounded window — and ``extra="forbid"``
    means a model cannot add a recipient. Recipient intent has no sealed source in
    this repository, so a recipient criterion built from ``guests`` is a projection
    with no intent behind it, and it stays ``EXPERIMENTAL`` until a sealed
    recipient field exists.
    """
    assert set(UnderstandingOutput.model_fields) == {"intent", "guests", "window"}
    assert get_args(UnderstandingOutput.model_fields["intent"].annotation) == (
        "schedule_meeting",
    )
    bounds = [
        (
            marker.__class__.__name__,
            getattr(marker, "min_length", None),
            getattr(marker, "max_length", None),
        )
        for marker in UnderstandingOutput.model_fields["guests"].metadata
    ]
    assert bounds == [("MinLen", 1, None), ("MaxLen", None, 5)]
    for name in UnderstandingOutput.model_fields:
        assert not {"to", "recipient", "recipients", "email"} & {name.lower()}, name
    # The prompt says the same thing, so the model is not asked for a field the
    # schema would then have to invent a meaning for.
    assert 'intent is always "schedule_meeting"' in UNDERSTANDING_SYSTEM_PROMPT
    assert "recipient" not in UNDERSTANDING_SYSTEM_PROMPT.lower()
    assert "email" not in UNDERSTANDING_SYSTEM_PROMPT.lower()
    # And no durable column could hold one either.
    assert not {"recipients", "to", "recipient"} & set(
        RuntimeExecution.__table__.columns.keys()
    )


def test_the_gmail_branch_never_reads_a_meaning_for_its_identity_row() -> None:
    """``required_recipients`` is the same value under a different word.

    The dispatch reads the window in exactly one place — the calendar branch — and
    the gmail branch is handed the identities alone. So the two payloads are the
    same construction over the same sealed row, and whatever distinguishes them is
    the profile name and nothing else. That is the precise shape of the open
    question: not "where does the value come from" (answered, and identically in
    both) but "what licenses calling it a recipient" (unanswered anywhere).
    """
    source = _code_of(_fulfillment_outcome)
    assert source.count("window=scenario.window") == 1
    assert source.count("gmail_send_criteria(") == 1
    assert source.count("identities=identities") == 2
    for absent in ("scenario.guests", "scenario.intent"):
        assert absent not in source, absent

    # The same row, the two domains, and only the field name differs.
    assert (
        _calendar_criteria().for_calendar_meeting().required_attendees
        == _gmail_criteria().for_gmail_send().required_recipients
    )
    # A send contract is still decided from a meeting-shaped row, because that is
    # the only understanding the runtime can produce.
    assert evaluate_gmail_send(
        FulfillmentInput(criteria=_gmail_criteria(), evidence=(_sent(),))
    ) is FulfillmentVerdict.FULFILLED
    assert (
        _fulfillment_outcome(
            _task_row(fulfillment_profile="gmail_send"),
            scenario=None,  # type: ignore[arg-type]
            identities=list(IDENTITIES),
            evidence=[_sent()],
        ).profile
        == "gmail_send"
    )


def test_a_gmail_contract_is_sealable_but_the_runtime_never_seals_one() -> None:
    """The vocabulary permits a send contract; no code path produces one.

    Worth stating because it is the *second* half of the authority gap: even if
    the recipient semantics were settled, the only builder in the repository
    stamps ``calendar_meeting`` on every task it creates, so a ``gmail_send``
    criteria today has no contract behind it stating a recipient was wanted. The
    gap is recorded, not repaired: sealing a send contract would mean expanding
    the live slice, which is explicitly out of scope for this round.
    """
    contract = TaskContractInput(
        goal="email Ahmed the quarterly plan",
        success_criteria=["message sent to the requested contact"],
        fulfillment_profile="gmail_send",
        fallback="ask the user",
    )
    assert contract.fulfillment_profile == "gmail_send"
    assert build_slice_contract("email Ahmed the quarterly plan").fulfillment_profile == (
        SLICE_FULFILLMENT_PROFILE
    ) == "calendar_meeting"
    # The criteria builder accepts the profile the dispatch supplies; it does not
    # derive one from the goal or from a tool list.
    assert _gmail_criteria().profile == "gmail_send"


# --- 4. criterion selection: what the code supports, and what it refutes -------


def test_the_request_carries_the_same_addresses_the_criteria_name() -> None:
    """The recorded refutation: the request is *not* free of the resolved row.

    This was stated in the source documentation as the reason the address anchor
    is a criterion — "the resolved identity row, which the request does not
    carry". Direct inspection of the live producer says otherwise:
    ``create_intent_args`` fills ``attendees`` from the very identities the
    criteria are sealed from, so the request carries a copy of the sealed row and
    the stated reason is false for calendar and untestable for gmail (no live send
    path exists to inspect).

    The criterion is still sound — it compares the durable *request* against the
    durable *sealed row*, and the log is not required to have been written by that
    one producer — but it is sound for a reason the code supports, and the false
    reason is not load-bearing for anything. Pinned so it cannot be reintroduced.
    """
    rows = [{"name": "Ahmed", "email": "Ahmed@Example.com"}]
    criteria = _calendar_criteria(identities=rows).for_calendar_meeting()
    args = create_intent_args(
        slot=(date(2026, 6, 24), time(17, 0), time(18, 0)),
        title="KAI meeting",
        timezone="UTC",
        attendee_emails=[item["email"] for item in rows if item.get("email")],
    )
    assert {address.casefold() for address in args["attendees"]} == (
        criteria.required_attendees
    )
    # Which makes the anchor redundant on the live path — see the next test for
    # what that leaves carrying the calendar domain's second field.
    assert "attendees" in args
    assert "identities" not in args and "resolution" not in args


def test_the_range_is_the_only_criterion_the_live_path_can_fail() -> None:
    """A provider-reported slot outside the window passes verification, and fails.

    Built from the real translator and the real verifier rather than from a
    fixture, because this is the case that gives the second calendar field its
    reason: the request's instant comes from the provider's own free-slot report,
    so nothing about the request can be wrong with respect to a range the intent
    asked for. Verification compares the read-back to the request, the request
    satisfies the address criterion by construction, and the sealed range is the
    only thing left that can say no.

    The mirror image is the address anchor: ``verify_event`` compares the
    read-back's people to the request with set equality, and the request's people
    are the sealed identities, so on this single producer no record that
    qualifies as a witness can fail the attendee check. The check is kept because
    the log is not required to come from that producer — but it is *not* the
    operative reason either domain carries a field, and saying so would be
    overstating what the live path shows.
    """
    outside = _read_slot_items(
        [
            {
                "tool": "calendar.read_slots",
                "outcome": "EXECUTION_SUCCEEDED",
                "ok": True,
                "data": {
                    "free": [
                        {
                            "start": "2026-06-30T09:00:00+00:00",
                            "end": "2026-06-30T10:00:00+00:00",
                        }
                    ]
                },
            }
        ]
    )
    slot = pick_slot(outside)
    assert slot is not None
    day, start, end = slot
    assert day == date(2026, 6, 30) and day > WINDOW.end_date

    args = create_intent_args(
        slot=slot,
        title="KAI meeting",
        timezone="UTC",
        attendee_emails=["ahmed@example.com"],
    )
    criteria = _calendar_criteria().for_calendar_meeting()
    record = _created(args=args, evidence={"event_id": "evt-outside"})
    # A record the provider is happy with...
    request = CreateEventRequest(
        title=args["title"],
        start=datetime(2026, 6, 30, 9, 0, tzinfo=UTC),
        end=datetime(2026, 6, 30, 10, 0, tzinfo=UTC),
        timezone="UTC",
        attendees=tuple(args["attendees"]),
    )
    readback = CalendarEvent(
        event_id="evt-outside",
        summary=args["title"],
        start=datetime(2026, 6, 30, 9, 0, tzinfo=UTC),
        end=datetime(2026, 6, 30, 10, 0, tzinfo=UTC),
        start_timezone="UTC",
        end_timezone="UTC",
        attendees=tuple(CalAttendee(email=address) for address in args["attendees"]),
    )
    assert verify_event(readback, request) == ()
    # ...is still not a meeting the intent asked for.
    assert evaluate_calendar_meeting(
        FulfillmentInput(criteria=SuccessCriteria(
            profile="calendar_meeting", calendar_meeting=criteria
        ), evidence=(record,))
    ) is FulfillmentVerdict.NOT_FULFILLED

    # And the mirror: a read-back that drops a sealed attendee is not even a
    # witness, because verification compares people to the request by equality.
    partial = CalendarEvent(
        event_id="evt-partial",
        summary=args["title"],
        start=datetime(2026, 6, 30, 9, 0, tzinfo=UTC),
        end=datetime(2026, 6, 30, 10, 0, tzinfo=UTC),
        start_timezone="UTC",
        end_timezone="UTC",
        attendees=(CalAttendee(email="someone.else@example.com"),),
    )
    assert "attendees" in {mismatch.field for mismatch in verify_event(partial, request)}


def test_the_evaluator_decides_on_the_range_and_not_on_the_title() -> None:
    """The anchor is what the evaluator decides on, not every sealed field.

    ``MeetingWindow`` seals four fields, and a sealed field existing is not
    sufficient for the live-sealing license: the calendar anchor is the three
    the evaluator actually reads. ``title`` is sealed and inert — it is carried
    for the created event and no verdict reads it — so it must not be able to
    satisfy the invariant on its own. This pins the "decided on" half
    structurally, the one thing a static profile-level license cannot see by
    itself.
    """
    import kai.runtime.fulfillment as F

    assert {
        "start_date",
        "end_date",
        "earliest_time",
        "title",
    } <= set(MeetingWindow.model_fields)
    decided = _code_of(F._meeting_satisfies_criteria)
    for field in ("start_date", "end_date", "earliest_time"):
        assert f"window.{field}" in decided, field
    assert "window.title" not in decided
    assert ".title" not in decided


def test_title_and_subject_are_refused_on_different_grounds() -> None:
    """Two exclusions, two genuinely different reasons — and only one is a copy.

    ``title`` does have a sealed source (``MeetingWindow.title``), and the
    provider already compares it against the request, so the "it is a copy of the
    intent" argument is not what excludes it. What excludes it is that no sealed
    success criterion states it *and* the read-after-create comparison already
    covers it.

    ``subject`` and ``body`` are the opposite case: no sealed source exists
    anywhere for either, in any model or column, so a criterion for them would
    have to be read back from the request itself. The refutation of the copy
    argument in the previous test applies to them only by accident, and it is
    worth not resting the argument on it.
    """
    # title: sealed source present, provider comparison present, prose silent.
    assert "title" in MeetingWindow.model_fields
    assert "title" not in set(CalendarMeetingCriteria.model_fields)
    request = CreateEventRequest(
        title="KAI meeting",
        start=datetime(2026, 6, 24, 16, 0, tzinfo=UTC),
        end=datetime(2026, 6, 24, 17, 0, tzinfo=UTC),
        timezone="UTC",
        attendees=("ahmed@example.com",),
    )
    retitled = CalendarEvent(
        event_id="evt-1",
        summary="Something else entirely",
        start=datetime(2026, 6, 24, 16, 0, tzinfo=UTC),
        end=datetime(2026, 6, 24, 17, 0, tzinfo=UTC),
        start_timezone="UTC",
        end_timezone="UTC",
        attendees=(CalAttendee(email="ahmed@example.com"),),
    )
    assert "title" in {mismatch.field for mismatch in verify_event(retitled, request)}
    prose = " ".join(SLICE_SUCCESS_CRITERIA).lower()
    assert "title" not in prose

    # subject / body: request-side only. No sealed source to compare them against.
    assert {"to", "subject", "body"} <= set(GmailSendArgs.model_fields)
    sealed_fields = (
        set(MeetingWindow.model_fields)
        | set(UnderstandingOutput.model_fields)
        | set(TaskContractInput.model_fields)
    )
    assert not {"subject", "body", "recipients"} & sealed_fields
    assert not {"subject", "body"} & set(RuntimeExecution.__table__.columns.keys())
    assert set(GmailSendCriteria.model_fields) == {"required_recipients"}


# --- 5. cardinality, and the correspondence it would need ---------------------


def test_the_two_field_counts_share_one_anchor_and_differ_by_a_range() -> None:
    """Cardinality here is 1 + 1, not 2 versus 1, and the extra is domain meaning.

    The two builders contain the *same* address construction, so the shared
    anchor is one fact about how a criterion is sealed, not two. Calendar carries
    one more field because its intent asks for a bounded window while its request
    receives one concrete instant — a difference in what the intent is, not in how
    it is written down. That is a domain difference with a stated cause, which is
    the most this pair of domains can support; a *predictive* law about
    cardinality would need a third case to be anything but a fit.
    """
    assert set(CalendarMeetingCriteria.model_fields) == {
        "window",
        "required_attendees",
    }
    assert set(GmailSendCriteria.model_fields) == {"required_recipients"}
    shared = "str(item['email']).casefold() for item in identities if item.get('email')"
    assert shared in _code_of(calendar_meeting_criteria)
    assert shared in _code_of(gmail_send_criteria)
    # The extra field is the range, and the request provably cannot carry one.
    args = create_intent_args(
        slot=(date(2026, 6, 24), time(17, 0), time(18, 0)),
        title="KAI meeting",
        timezone="UTC",
        attendee_emails=["ahmed@example.com"],
    )
    assert {"date", "start_time"} <= set(args)
    assert not {"start_date", "end_date", "earliest_time"} & set(args)


def test_the_calendar_fields_correspond_to_sealed_prose_and_the_gmail_field_has_none() -> None:
    """The one correspondence the code supports, and where it stops.

    Calendar's two fields are the two of its three sealed prose criteria that are
    not the existential "an event was created" — the evaluator's quantifier
    answers that one, and no field may compete with it. The gmail payload has no
    such correspondence available: no sealed contract in the repository carries
    the ``gmail_send`` profile, so nothing states that a recipient was wanted.
    That is why the shared construction above is evidence of a shared *mechanism*
    and not of a shared *meaning*.
    """
    prose = " ".join(SLICE_SUCCESS_CRITERIA).lower()
    assert "attendee" in prose
    assert "window" in prose
    assert "calendar event created" in prose
    # Which is the quantifier, and which must stay out of the payload.
    assert set(CalendarMeetingCriteria.model_fields) == {
        "window",
        "required_attendees",
    }
    assert set(SLICE_SUCCESS_CRITERIA) == {
        "calendar event created",
        "attendee is the requested contact",
        "event fits the requested time window",
    }
    # Nothing states a recipient, because nothing seals a send contract.
    assert not any("recipient" in item.lower() for item in SLICE_SUCCESS_CRITERIA)
    assert SLICE_FULFILLMENT_PROFILE == "calendar_meeting"


# --- 6. recovery: criteria + evidence are the whole of the re-derivation -------


def test_the_recorded_outcome_is_never_an_input_to_the_criteria_or_the_verdict() -> None:
    """The audit triple describes the settlement; it cannot decide one.

    Both halves matter. The dispatch does not read a recorded verdict or a
    recorded version — it re-derives and overwrites — and a row carrying a
    verdict that contradicts its evidence settles the other way. A recovery
    therefore needs nothing but the two sealed rows and the durable log: no
    stored decision, no model, no provider.
    """
    source = _code_of(_fulfillment_outcome)
    for absent in ("fulfillment_verdict", "row.evaluator_version"):
        assert absent not in source, absent

    contradictory = _sent(args={"to": "wrong@example.com"})
    settled = _fulfillment_outcome(
        _task_row(fulfillment_profile="gmail_send"),
        scenario=None,  # type: ignore[arg-type]
        identities=list(IDENTITIES),
        evidence=[contradictory],
    )
    assert settled.verdict is FulfillmentVerdict.NOT_FULFILLED
    # Same rows, any number of times: the re-derivation a crash triggers lands in
    # the same place, which is the whole of what recovery needs.
    assert {
        _fulfillment_outcome(
            _task_row(fulfillment_profile="gmail_send"),
            scenario=None,  # type: ignore[arg-type]
            identities=list(IDENTITIES),
            evidence=[contradictory],
        )
        for _ in range(3)
    } == {settled}
