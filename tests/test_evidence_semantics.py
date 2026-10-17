"""What makes an evidence record a *qualifying creation attempt* — stated once.

```text
raw evidence record
     -> qualifying_creation(record)  ->  QualifyingCreation | None
     -> relevant witness set
     -> SuccessCriteria -> evaluator -> FulfillmentOutcome
```

The gap these close: the repository held two readings of "a meeting was
created". The fulfillment evaluator filtered the durable log on
``evidence["event_id"]``; the runtime's own ``_created_event_id`` filtered on the
same field *plus* a ``data["event_id"]`` fallback and without requiring ``ok``. So
one record could be a witness to the evaluator and invisible — or visible — to
the code that decides which created event to read back. That is a semantic
boundary with two answers, not an ordering defect.

These tests pin the single answer, and then pin that both readers now take it.
No database, no HTTP, no model, no IR: the whole boundary is a pure function of a
record's own content, and that is exactly what is asserted.
"""

from __future__ import annotations

import ast
import inspect
import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import date, time
from types import MappingProxyType, SimpleNamespace
from typing import Any, Final

import pytest

from kai.runtime.evidence import (
    QualifyingCreation,
    qualifying_creation,
    qualifying_creations,
)
from kai.runtime.fulfillment import (
    FulfillmentInput,
    FulfillmentOutcome,
    FulfillmentVerdict,
    calendar_meeting_criteria,
    evaluate_calendar_meeting,
)
from kai.runtime.schemas import MeetingWindow
from kai.runtime.service import _created_event_id, _fulfillment_outcome

WINDOW: Final = MeetingWindow(
    start_date=date(2026, 6, 22),
    end_date=date(2026, 6, 26),
    earliest_time=time(16, 0),
    title="KAI meeting",
)

IDENTITIES: Final = ({"email": "Ahmed@Example.com", "name": "Ahmed"},)

GOOD_ARGS: Final = {
    "date": "2026-06-24",
    "start_time": "17:30",
    "attendees": ["ahmed@example.com"],
}


def _record(**overrides: Any) -> dict[str, Any]:
    """One durable tool-run record of a verified, provider-identified creation.

    Shaped exactly as :func:`kai.ir.adapter.evidence_from_observation` writes it,
    including the ``data`` copy of the serialized provider payload — so the
    ``data``/``evidence`` question is exercised on the real record shape rather
    than on a convenient one.
    """
    record: dict[str, Any] = {
        "step_order": 3,
        "tool": "calendar.create_event",
        "args": dict(GOOD_ARGS),
        "outcome": "EXECUTION_SUCCEEDED",
        "ok": True,
        "verified": True,
        "error_code": None,
        "message": "",
        "data": {
            "event_id": "evt-1",
            "title": "KAI meeting",
            "start": "2026-06-24T17:30:00+03:00",
            "end": "2026-06-24T18:30:00+03:00",
            "attendees": ["ahmed@example.com"],
        },
        "evidence": {"contract": "read_after_create", "event_id": "evt-1",
                     "mismatch_count": 0},
        "request_id": "runtime-1-3-0",
        "ir_action_id": "runtime-1-3-0",
    }
    record.update(overrides)
    return record


def _criteria() -> Any:
    return calendar_meeting_criteria(
        profile="calendar_meeting", window=WINDOW, identities=IDENTITIES
    )


def _verdict(*evidence: dict[str, Any]) -> FulfillmentVerdict:
    return evaluate_calendar_meeting(
        FulfillmentInput(criteria=_criteria(), evidence=tuple(evidence))
    )


def _permutations(items: list[dict[str, Any]]) -> list[tuple[dict[str, Any], ...]]:
    if len(items) <= 1:
        return [tuple(items)]
    out: list[tuple[dict[str, Any], ...]] = []
    for index, item in enumerate(items):
        rest = items[:index] + items[index + 1 :]
        for tail in _permutations(rest):
            out.append((item, *tail))
    return out


def _source_of(function: Callable[..., Any]) -> str:
    tree = ast.parse(inspect.getsource(function))
    node = tree.body[0]
    if (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    ):
        node.body.pop(0)
    return ast.unparse(node)


def _witness_key(witness: QualifyingCreation) -> str:
    """A comparable stand-in for a witness, which is frozen but not hashable.

    The witness carries the attempt's arguments, so equality is what identifies
    it and no content hash is defined for it. Comparing witnesses is done by
    value; this renders one deterministically so a *set* of them can be compared
    across orderings, which is the property under test.
    """
    return json.dumps(
        {
            "provider_event_id": witness.provider_event_id,
            "arguments": dict(witness.arguments),
        },
        sort_keys=True,
    )


def _witness_keys(evidence: Any) -> frozenset[str]:
    return frozenset(_witness_key(witness) for witness in qualifying_creations(evidence))


# --- A. what qualification requires -------------------------------------------


def test_a_verified_provider_identified_creation_qualifies() -> None:
    """The positive case, stated as the whole rule at once."""
    witness = qualifying_creation(_record())
    assert isinstance(witness, QualifyingCreation)
    assert witness.provider_event_id == "evt-1"
    assert witness.arguments == GOOD_ARGS
    # And the record this came from is a real shape, not a convenient one.
    assert qualifying_creations([_record()]) == (witness,)


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"tool": "calendar.read_slots"}, "a different tool"),
        ({"tool": "calendar.get_event"}, "reading a meeting is not creating one"),
        ({"tool": "gmail.send"}, "an unrelated tool"),
        ({"outcome": "EXECUTION_FAILED"}, "the execution failed"),
        ({"outcome": "REQUIRES_ATTENTION"}, "verification did not pass"),
        ({"outcome": "DENIED"}, "policy refused the attempt"),
        ({"outcome": "APPROVAL_REQUIRED"}, "the attempt is still parked"),
        ({"ok": False}, "the Gateway reported not-ok"),
        ({"verified": False}, "the verification boundary did not stand behind it"),
        ({"evidence": {}}, "no provider identity at all"),
        ({"evidence": {"contract": "read_after_create"}}, "contract, but no identity"),
        ({"evidence": {"event_id": ""}}, "an empty identity is not an identity"),
        ({"evidence": {"event_id": "   "}}, "whitespace is not an identity"),
        ({"evidence": {"event_id": None}}, "no identity"),
        ({"evidence": {"event_id": 42}}, "not an identity string"),
        ({"evidence": {"event_id": ["evt-1"]}}, "not an identity string"),
        ({"evidence": None}, "no evidence container"),
        ({"evidence": "evt-1"}, "not an evidence container"),
    ],
)
def test_everything_else_is_non_qualifying(overrides: dict[str, Any], why: str) -> None:
    """Each condition is necessary, and each fails on its own.

    Qualification is a conjunction, so this walks it term by term: no single
    condition may be dropped without some record here flipping to ``None``.
    """
    assert qualifying_creation(_record(**overrides)) is None, why
    # And no record like it can vote on fulfillment either.
    assert _verdict(_record(**overrides)) is FulfillmentVerdict.NOT_FULFILLED, why


@pytest.mark.parametrize(
    "record", ["not a dict", 42, None, ["a", "list"], {"tool": "t"}], ids=repr
)
def test_qualification_is_total_and_never_raises(record: Any) -> None:
    """Anything at all is answerable.

    The durable column is JSON, so a historical or hand-edited row can hold
    something that is not a record. Qualification refuses it — as it refuses
    every other non-qualifying thing — rather than raising inside an evaluator
    that is required to be total.
    """
    assert qualifying_creation(record) is None
    assert qualifying_creations([record, _record()]) == (qualifying_creation(_record()),)


# --- B. the ``event_id`` decision, taken explicitly -----------------------------


def test_the_provider_identity_is_read_from_evidence_and_never_from_data() -> None:
    """``data.event_id`` alone does not qualify, and that is the decision.

    ``evidence`` is what the verification boundary republishes after re-asserting
    the tool's ``READ_AFTER_CREATE`` contract; ``data`` is the adapter's
    serialized provider payload, which the Gateway publishes without consulting
    that contract at all. Only one of the two is a claim that something was
    *proved*, so only one of them can stand behind a fulfillment verdict.
    """
    data_only = _record(evidence={}, data={"event_id": "evt-from-data-only"})
    assert (data_only["evidence"] or {}).get("event_id") is None
    assert data_only["data"]["event_id"] == "evt-from-data-only"
    # Not a witness, for the evaluator...
    assert qualifying_creation(data_only) is None
    assert _verdict(data_only) is FulfillmentVerdict.NOT_FULFILLED
    # ...and not an address for the runtime to read back either. Before the
    # boundary was shared, this record was a creation to one reader and not to
    # the other, which is the whole defect.
    assert _created_event_id([data_only]) is None


def test_a_disagreeing_data_cannot_overturn_the_authoritative_evidence() -> None:
    """When the two copies disagree, ``evidence`` wins without comment."""
    record = _record(evidence={"event_id": "evt-authoritative"},
                     data={"event_id": "evt-from-payload"})
    witness = qualifying_creation(record)
    assert witness is not None
    assert witness.provider_event_id == "evt-authoritative"
    assert _created_event_id([record]) == "evt-authoritative"


def test_the_two_copies_may_agree_without_becoming_interchangeable() -> None:
    """Agreement today is not identity of meaning.

    The one adapter that writes both writes the same value into both, so most
    records satisfy the policy by coincidence. That coincidence is not the reason
    the policy is safe: the fallback is refused whether or not the copies happen
    to match, which is what keeps the rule from depending on an adapter's
    internals.
    """
    agreeing = _record()
    assert agreeing["data"]["event_id"] == agreeing["evidence"]["event_id"]
    assert qualifying_creation(agreeing) is not None
    # Remove the authoritative copy and the same record stops qualifying, with
    # nothing else about it changed.
    stripped = _record(evidence={})
    assert stripped["data"] == agreeing["data"]
    assert qualifying_creation(stripped) is None


def test_only_the_evidence_container_is_consulted_for_the_identity() -> None:
    """The decision is a single read, stated in the code as well as in prose.

    ``data`` appears nowhere in the qualification path, so the fallback cannot
    creep back in as a convenience.
    """
    from kai.runtime import evidence as module

    body = _source_of(module.qualifying_creation) + _source_of(module._provider_event_id)
    assert "_PROVIDER_IDENTITY_CONTAINER" in body
    assert module._PROVIDER_IDENTITY_CONTAINER == "evidence"
    assert '"data"' not in body and "'data'" not in body
    assert "record.get(\"data\")" not in body


# --- C. the model -------------------------------------------------------------


def test_the_model_carries_the_identity_and_the_arguments_and_nothing_else() -> None:
    """Two fields, because those are the two things a witness *is*.

    Anything else — a step, a request id, an action id, an ordinal — would name
    where the record sat in a log rather than what it witnesses, and no reader
    consults it. An identifier kept only for audit is a second thing that can
    disagree with the durable row it claims to describe.
    """
    assert set(QualifyingCreation.__dataclass_fields__) == {
        "provider_event_id",
        "arguments",
    }
    assert "step_order" not in QualifyingCreation.__dataclass_fields__
    assert "request_id" not in QualifyingCreation.__dataclass_fields__
    assert "ir_action_id" not in QualifyingCreation.__dataclass_fields__


def test_the_model_is_frozen_and_holds_a_read_only_copy() -> None:
    """A witness cannot be edited, and the record cannot reach in and edit it."""
    witness = qualifying_creation(_record())
    assert isinstance(witness, QualifyingCreation)
    with pytest.raises(FrozenInstanceError):
        witness.provider_event_id = "evt-2"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        witness.arguments = {}  # type: ignore[misc]
    with pytest.raises(TypeError):
        witness.arguments["date"] = "2027-01-01"  # type: ignore[index]
    assert isinstance(witness.arguments, MappingProxyType)

    # The copy is taken at qualification time, so mutating the source record
    # afterwards cannot rewrite a witness that has already been decided on.
    record = _record()
    witness = qualifying_creation(record)
    record["args"]["date"] = "2030-01-01"
    assert witness is not None
    assert witness.arguments["date"] == "2026-06-24"


def test_the_witness_carries_the_request_not_the_provider_read_back() -> None:
    """``args`` is what was asked for; ``data`` is what came back.

    The evaluator compares the meeting that was *witnessed* against the sealed
    criteria, and the criteria were sealed against the request. Reading the
    provider payload instead would be a change of meaning, so the model is
    explicit about which of the two it holds.
    """
    record = _record(
        args={"date": "2026-06-24", "start_time": "17:30", "attendees": ["a@b.com"]},
        data={"event_id": "evt-1", "start": "2026-06-25T09:00:00+03:00"},
    )
    witness = qualifying_creation(record)
    assert witness is not None
    assert witness.arguments["date"] == "2026-06-24"
    assert witness.arguments["start_time"] == "17:30"


def test_the_model_needs_no_persistence() -> None:
    """A typed projection of durable evidence, so there is nothing to store.

    Every input qualification reads is already on ``RuntimeExecution.evidence``,
    committed before verification. Persisting the projection would add a copy that
    can drift from the log it was derived from, a migration, and a backfill
    question about rows written before this meaning was stated — and the answer
    to that question would be a guess, since qualification is derived, not
    recorded.
    """
    from kai.runtime.models import RuntimeExecution

    persisted = set(RuntimeExecution.__table__.columns.keys())
    assert "evidence" in persisted
    assert not {
        "qualifying_creation",
        "qualifying_creations",
        "witnesses",
        "provider_event_id",
    } & persisted


# --- D. determinism, order, duplicates -----------------------------------------


def test_qualification_is_a_function_of_the_record_content_alone() -> None:
    """The same record qualifies the same way, always.

    Repeated calls, a fresh read of the same record, and a round trip through the
    durable JSON representation all agree. Nothing about *when* or *where* the
    read happens is an input, which is what lets a recovery re-derive a crashed
    run's answer.
    """
    record = _record()
    assert len({_witness_key(qualifying_creation(record)) for _ in range(5)}) == 1
    stored = json.loads(json.dumps(record))
    assert qualifying_creation(stored) == qualifying_creation(record)
    # Mutating an unrelated field cannot move it, either.
    other = _record(request_id="runtime-9-9-9", step_order=99)
    assert qualifying_creation(other) == qualifying_creation(record)


def test_the_witness_set_is_independent_of_the_order_records_arrived_in() -> None:
    """Every permutation of one log yields one witness set.

    Not merely the same *count* and not merely the same first element: the same
    set of values, so nothing downstream can be sensitive to a log's order even
    if it were.
    """
    log = [
        _record(),
        _record(evidence={"event_id": "evt-2"}, args={**GOOD_ARGS, "date": "2026-06-30"}),
        _record(ok=False),
        _record(tool="calendar.read_slots"),
        _record(evidence={}),
    ]
    orders = _permutations(log)
    assert len(orders) == 120
    assert len({_witness_keys(order) for order in orders}) == 1
    assert _witness_keys(log) == _witness_keys(
        [_record(evidence={"event_id": "evt-2"}, args={**GOOD_ARGS, "date": "2026-06-30"}),
         _record()]
    )


def test_duplicates_change_neither_the_witness_set_nor_the_verdict() -> None:
    """A repeated record is one meeting, and a repeat is not a new witness.

    Qualification applies no duplicate suppression, and needs none: the filter is
    a pure function of one record, so recording the same creation twice yields
    the same witness twice, and the verdict is existential over the set. A
    dedupe pass would be a storage and reporting concern, not a correctness one.
    """
    record = _record()
    assert len(qualifying_creations([record])) == 1
    assert len(qualifying_creations([record, record, record])) == 3
    # A repeat adds nothing to the set of distinct witnesses...
    assert _witness_keys([record, record, record]) == _witness_keys([record])
    # ...and one verdict, in every arrangement.
    assert {_verdict(*order) for order in _permutations([record, record])} == {
        FulfillmentVerdict.FULFILLED
    }


# --- E. one boundary, two readers ----------------------------------------------


def test_the_evaluator_and_the_runtime_read_the_same_qualification() -> None:
    """Both readers are the same function, not two that happen to agree today.

    The evaluator's relevant set and the runtime's read-back address are now both
    computed by :func:`qualifying_creations`, so a record cannot be a witness to
    one and not to the other, and the ``data`` fallback cannot exist in one
    reader and not the other.
    """
    from kai.runtime import fulfillment, service

    assert "qualifying_creations" in inspect.getsource(fulfillment.evaluate_calendar_meeting)
    assert "qualifying_creations" in inspect.getsource(service._created_event_id)
    # Neither reader re-implements the rule: neither names the tool outcome, nor
    # reaches into a raw record for an id or for the arguments.
    for reader in (
        _source_of(fulfillment.evaluate_calendar_meeting),
        _source_of(service._created_event_id),
    ):
        assert "EXECUTION_SUCCEEDED" not in reader
        assert '"data"' not in reader and '"args"' not in reader
        assert ".get(" not in reader
    # ...and the old private filter is gone rather than left as a second answer.
    assert not hasattr(fulfillment, "_qualifying_creations")
    assert "_MEETING_CREATION_TOOL" not in inspect.getsource(fulfillment)


def test_the_two_readers_agree_on_every_record_shape() -> None:
    """Swept across the whole conjunction, both readers say the same thing.

    Qualification is a witness; the runtime's helper is a *choice* of one witness
    to address. Where a witness exists at all, the address must be that witness's
    own provider id — never an identity from a record no witness came from.
    """
    cases: list[dict[str, Any]] = [
        _record(),
        _record(tool="calendar.read_slots"),
        _record(outcome="EXECUTION_FAILED"),
        _record(ok=False),
        _record(verified=False),
        _record(evidence={}),
        _record(evidence={}, data={"event_id": "evt-from-data-only"}),
        _record(evidence={"event_id": "evt-2"}),
        {"tool": "calendar.create_event"},
        {"junk": True},
    ]
    for record in cases:
        witnesses = qualifying_creations([record])
        address = _created_event_id([record])
        assert address == (
            witnesses[0].provider_event_id if witnesses else None
        ), record
        # A witness in the set implies the evaluator considers the "a meeting was
        # created" condition satisfied to be worth checking, and the criteria
        # then decide on its own.
        expected = (
            FulfillmentVerdict.FULFILLED
            if witnesses
            else FulfillmentVerdict.NOT_FULFILLED
        )
        if witnesses:
            expected = _verdict(record)
        assert _verdict(record) is expected, record


def test_the_runtime_addresses_the_earliest_witness_in_an_append_only_log() -> None:
    """A sequencing choice, made inside the witness set and never outside it.

    The runtime must name one meeting to read back, and the log is append-only,
    so the earliest qualifying creation is the one. That is a choice *among*
    witnesses, which is why it is allowed to be order-sensitive while the verdict
    is not — and it is still a choice made from the witness set, so it can never
    address a meeting no witness exists for.
    """
    first = _record(evidence={"event_id": "evt-first"})
    second = _record(evidence={"event_id": "evt-second"})
    assert _created_event_id([first, second]) == "evt-first"
    assert _created_event_id([second, first]) == "evt-second"
    # A non-qualifying record ahead of them is passed over rather than addressed.
    noise = _record(ok=False, evidence={"event_id": "evt-failed"})
    assert _created_event_id([noise, first, second]) == "evt-first"
    assert _created_event_id([noise]) is None


# --- F. recovery, replay, and re-derivation ------------------------------------


def test_the_same_durable_evidence_re_derives_the_same_everything() -> None:
    """Live run, recovery, and replay of a permuted log agree on all of it.

    This is the property the whole boundary rests on: the durable evidence and the
    sealed criteria are the only inputs, so re-reading them — after a crash, in a
    different process, in a scrambled order — must produce the same witnesses, the
    same read-back address set, and the same ``FulfillmentOutcome``.
    """
    row = {
        "goal": "schedule a meeting",
        "fulfillment_profile": "calendar_meeting",
    }
    scenario = SimpleNamespace(window=WINDOW)
    live = [
        _record(ok=False, evidence={"event_id": "evt-attempt"}),
        _record(tool="contacts.match"),
        _record(tool="calendar.read_slots"),
        _record(evidence={"event_id": "evt-2"}, args={**GOOD_ARGS, "date": "2026-06-30"}),
        _record(),
    ]
    assert len(live) == 5

    baseline_outcome = _fulfillment_outcome(
        SimpleNamespace(**row),
        scenario=scenario,
        identities=list(IDENTITIES),
        evidence=live,
    )
    assert baseline_outcome.verdict is FulfillmentVerdict.FULFILLED
    baseline_witnesses = qualifying_creations(live)

    # A recovery re-reads the column. Same bytes, new objects.
    replayed = [json.loads(json.dumps(record)) for record in live]
    assert qualifying_creations(replayed) == baseline_witnesses
    assert _fulfillment_outcome(
        SimpleNamespace(**row),
        scenario=scenario,
        identities=[dict(item) for item in IDENTITIES],
        evidence=replayed,
    ) == baseline_outcome

    # And every permutation of the same log re-derives the same outcome.
    for permuted in _permutations(live):
        assert _witness_keys(permuted) == _witness_keys(live)
        assert _fulfillment_outcome(
            SimpleNamespace(**row),
            scenario=scenario,
            identities=list(IDENTITIES),
            evidence=list(permuted),
        ) == baseline_outcome


def test_a_record_removed_from_the_log_leaves_no_witness_to_read() -> None:
    """Losing the creation removes the witness and the address together.

    The runtime's read-back address and the evaluator's witness set are two
    projections of one boundary, so evidence that never survived a crash cannot
    be half-remembered by one of them.
    """
    log = [_record(tool="calendar.read_slots"), _record()]
    recovered = [record for record in log if record["tool"] == "calendar.create_event"]
    assert len(qualifying_creations(recovered)) == 1
    assert _created_event_id(recovered) == "evt-1"
    # And a log with no creation at all is honestly empty on both sides.
    assert qualifying_creations([_record(tool="calendar.read_slots")]) == ()
    assert _created_event_id([_record(tool="calendar.read_slots")]) is None
    assert _verdict(*[record for record in log if record["tool"] == "calendar.read_slots"]) is (
        FulfillmentVerdict.NOT_FULFILLED
    )


# --- G. what qualification is not ----------------------------------------------


def test_qualification_is_not_a_fulfillment_decision() -> None:
    """A witness can exist and the Intent can still be unfulfilled.

    These are two different questions and this module only answers the first. A
    qualifying creation that books the wrong day is a perfectly good witness and a
    ``NOT_FULFILLED`` run, and collapsing the two would turn "no meeting was
    created" into "the meeting was not the right one" — a different failure with
    a different owner.
    """
    misplaced = _record(args={**GOOD_ARGS, "date": "2026-06-30"})
    assert qualifying_creation(misplaced) is not None
    assert _verdict(misplaced) is FulfillmentVerdict.NOT_FULFILLED
    # The existential/conjunctive split is untouched: existence over the set,
    # conjunction within one witness.
    assert _verdict(misplaced, _record()) is FulfillmentVerdict.FULFILLED
    assert _verdict(misplaced) is FulfillmentVerdict.NOT_FULFILLED
    assert len(qualifying_creations([misplaced, _record()])) == 2


def test_the_model_cannot_be_widened_into_a_generic_evidence_language() -> None:
    """One domain, one predicate, no registry, no second payload.

    Stated as a test because the tempting next step is a generic ``Evidence`` or
    ``Witness`` type with a payload per domain. That is a schema decision nobody
    has made, and a shared evidence vocabulary across domains has its own
    authority questions. This is a closed predicate over one tool.

    ``undetermined_creations`` is the one name Phase 6 added, and it is here
    only because it is *narrower* than a generic vocabulary rather than a step
    towards one: it reads the same single tool, returns the same single record
    shape the log already holds, and answers a question about one domain. It is
    the complement of ``qualifying_creations``, not a second kind of witness, and
    ``QualifyingCreation`` is still exactly two fields.

    The ``task_preparation`` names are the Stage 2E addition, and they stay on the
    same side of the line: they read exactly one more tool (``task.prepare``),
    return the same durable record shape, and answer a question about one domain,
    with ``QualifyingPreparation`` exactly two fields as well. No generic
    ``Evidence``/``Witness``/registry vocabulary is introduced.
    """
    from kai.runtime import evidence as module

    assert module.MEETING_CREATION_TOOL == "calendar.create_event"
    assert module.TASK_PREPARATION_TOOL == "task.prepare"
    assert set(module.__all__) == {
        "MEETING_CREATION_TOOL",
        "TASK_PREPARATION_TOOL",
        "QualifyingCreation",
        "QualifyingPreparation",
        "qualifying_creation",
        "qualifying_creations",
        "qualifying_preparation",
        "qualifying_preparations",
        "undetermined_creations",
    }
    assert not {
        "Evidence",
        "Witness",
        "Registry",
        "resolve_witness",
        "evaluate",
        "profile",
    } & set(module.__all__)
    assert len(QualifyingCreation.__dataclass_fields__) == 2
    assert len(module.QualifyingPreparation.__dataclass_fields__) == 2


def test_the_outcome_record_is_unchanged_by_this_boundary() -> None:
    """Qualifying differently would have shown up here first.

    The durable audit triple is what a reader consults to ask "by what, and with
    what semantics", so a change in which records qualify is a change in
    ``calendar_meeting:v1``'s meaning and would require a version bump. It did
    not: the same records qualified before and qualify now, and the version says
    so.
    """
    outcome = _fulfillment_outcome(
        SimpleNamespace(goal="schedule a meeting", fulfillment_profile="calendar_meeting"),
        scenario=SimpleNamespace(window=WINDOW),
        identities=list(IDENTITIES),
        evidence=[_record()],
    )
    assert isinstance(outcome, FulfillmentOutcome)
    assert outcome.version == "calendar_meeting:v1"
    assert outcome.profile == "calendar_meeting"
    assert outcome.verdict is FulfillmentVerdict.FULFILLED
