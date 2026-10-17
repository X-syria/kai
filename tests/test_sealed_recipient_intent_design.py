"""The sealed recipient-intent representation, and why it is not implemented yet.

The previous round established the *absence*: no sealed artifact states that a
recipient was wanted, so ``required_recipients`` is a resolution projection and
``gmail_send`` is not licensed (``tests/test_recipient_intent_provenance.py``).
This round asks the constructive half — what exact value would represent *who
the user intends to receive this message*, where would it come from, how would it
be sealed, and why would it be an authority while the resolved address is not —
and then asks the question that decides implementation:

> can that value be introduced **minimally**, without touching the calendar
> path, and without a second live-sealing licence path?

The answer this file pins is **no**, for three reasons that are each mechanical
rather than editorial.

1. **The evaluator's only join key is an address.** A qualifying send witness
   carries exactly ``{to, subject, body}``, and the decision compares
   ``args["to"]`` against the sealed set. There is no other durable value in the
   evidence that could carry a name, so a sealed value the gmail evaluator can
   *decide on* must be an address or nothing.
2. **No sealed scenario can pose the question.** ``intent`` is
   ``Literal["schedule_meeting"]`` and the other two fields are calendar-shaped,
   so a recipient field added today would be a send claim inside a meeting
   claim, or a second statement of ``guests``. There is no reachable state in
   which it carries a recipient.
3. **Memory is already inside the understanding prompt.** A ``PERSON`` memory
   holding an address is passed verbatim to the understanding model while the
   goal in the same example holds none, so "the model produced this address" and
   "memory produced this address" are indistinguishable at the model boundary.
   The control therefore cannot be the prompt; it has to be a deterministic
   check that every sealed recipient occurs literally in ``<goal>``.

Together these fix the shape of the answer: the sealed value would have to be an
**address the user wrote in the goal**, admitted only by that deterministic
grounding check. The check is the whole warrant, it is not a prompt instruction,
and it can be stated today — but it can only be *reachable* once a second
understanding intent exists, which is a contract amendment rather than a field.

So this file changes no production code. It pins the premises, so that a round
which does implement the representation has to move each pin deliberately instead
of discovering it. Nothing here licenses ``gmail_send``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, get_args
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from kai.db.engine import async_session_factory
from kai.identity import IdentityQuery
from kai.ir.nodes import IntentNode
from kai.ir.program import canonical_fingerprint
from kai.memory.models import MemoryType
from kai.memory.service import create_memory
from kai.runtime.fulfillment import (
    FulfillmentInput,
    FulfillmentVerdict,
    GmailSendCriteria,
    _send_satisfies_criteria,
    calendar_meeting_criteria,
    evaluate_gmail_send,
    gmail_send_criteria,
)
from kai.runtime.schemas import RuntimeTaskResponse, UnderstandingOutput
from kai.runtime.send_evidence import qualifying_sends
from kai.runtime.service import AgentRuntime, _fulfillment_outcome
from kai.runtime.understanding import understand_scenario
from kai.tasks import service as task_service_module
from kai.tasks.schemas import FulfillmentProfile
from kai.tasks.service import LIVE_SEALABLE_PROFILES
from kai.tools.schemas import GmailSendArgs
from tool_harness import make_gateway, make_task, make_tenant_user

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALICE: Final = "alice@example.com"

#: A goal that names a person and states no address: the ordinary case, and the
#: one no representation can bind to a user-stated address.
SEND_GOAL: Final = "email Alice about the quarterly plan"

TZ: Final = "UTC"


class _StubModel:
    """A model that returns exactly what the test hands it, and records the call."""

    model_id = "stub-understanding-v0"

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[list[dict[str, Any]]] = []

    async def generate_plan(self, *, messages: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append(messages)
        return self.response


class _TaskRow:
    """The one row field the fulfillment dispatcher reads, and nothing else."""

    fulfillment_profile: FulfillmentProfile = "calendar_meeting"


def _future_window() -> dict[str, Any]:
    """A window always in the future, so only the rule under test can fail it."""
    start = date.today() + timedelta(days=3)
    return {
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=1)).isoformat(),
        "earliest_time": "09:00",
        "title": "KAI",
    }


def _scenario(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "intent": "schedule_meeting",
        "guests": ["Alice"],
        "window": _future_window(),
    }
    payload.update(overrides)
    return payload


def _sent(to: str) -> dict[str, Any]:
    """One qualifying, verified, provider-identified ``gmail.send`` attempt."""
    return {
        "tool": "gmail.send",
        "outcome": "EXECUTION_SUCCEEDED",
        "ok": True,
        "verified": True,
        "args": {"to": to, "subject": "Quarterly plan", "body": "hello"},
        "evidence": {"message_id": "m1"},
    }


def _criteria(*addresses: str):
    return gmail_send_criteria(
        profile="gmail_send",
        identities=[{"name": "Alice", "email": address} for address in addresses],
    )


# ---------------------------------------------------------------------------
# 1. The join key is an address, so the anchor has to be an address
# ---------------------------------------------------------------------------


def test_the_gmail_evaluator_can_only_decide_on_an_address() -> None:
    """A witness offers three arguments and exactly one of them is the join key.

    ``required_recipients`` is a set of addresses, the witness's ``to`` is the
    address the request carried, and the decision is set containment between the
    two. Nothing else in the record is readable as a recipient: ``subject`` and
    ``body`` are content, and the provider identity is not an address at all.

    So a sealed value the evaluator could decide on must *be* an address. A name
    has no join key here, and a name-shaped criterion is not weakly satisfied —
    it is unconditionally unsatisfied, which is what the second half pins.
    """
    witness = qualifying_sends([_sent(ALICE)])[0]
    assert sorted(witness.arguments) == ["body", "subject", "to"]

    # The address the request carried decides the verdict on its own.
    assert _send_satisfies_criteria(witness, criteria=_criteria(ALICE).for_gmail_send())

    # A sealed value naming the person rather than the address decides nothing,
    # and no other spelling of that name changes it.
    for sealed in ("Alice", "Alice Ahmed", "alice", f'"{ALICE}"', f"<{ALICE}>"):
        criteria = _criteria(sealed).for_gmail_send()
        assert not _send_satisfies_criteria(witness, criteria=criteria), sealed

    # So the same evidence is FULFILLED under one sealed value and NOT_FULFILLED
    # under another that is not an address.
    record = (_sent(ALICE),)
    assert evaluate_gmail_send(FulfillmentInput(criteria=_criteria(ALICE), evidence=record)) is (
        FulfillmentVerdict.FULFILLED
    )
    assert evaluate_gmail_send(FulfillmentInput(criteria=_criteria("Alice"), evidence=record)) is (
        FulfillmentVerdict.NOT_FULFILLED
    )


def test_a_name_and_an_address_are_different_sealed_claims_not_one_value() -> None:
    """The representation has to hold the user's text, not a resolved party.

    The two readings of "send this to Alice" are kept apart by the schema that
    would carry them, and neither can be normalized into the other: an address is
    a claim the evaluator can act on, a name is not. This is why a
    resolution-shaped value cannot double as a sealed intent value — the moment
    an address is written into the intent layer, the distinction this round is
    about has already been lost and nothing downstream can recover it.

    And the one field that does hold user-named text today cannot carry the
    distinction either: it accepts a name and an address identically.
    """
    # A name is bounded display text, normalized the way the domain already does.
    assert UnderstandingOutput.model_validate(_scenario(guests=["Alice  Ahmed "])).guests == [
        "Alice Ahmed"
    ]
    # An address is not refused where text is expected, so ``guests`` cannot be
    # read as "people named and nothing more" — it cannot tell the two apart.
    assert UnderstandingOutput.model_validate(_scenario(guests=[ALICE])).guests == [ALICE]
    assert set(UnderstandingOutput.model_fields) == {"intent", "guests", "window"}


# ---------------------------------------------------------------------------
# 2. There is no sealed scene in which a recipient is a question
# ---------------------------------------------------------------------------


def test_no_sealed_scenario_can_state_a_send() -> None:
    """``intent`` is closed to one literal, and the rest of the shape is calendar.

    That is why a recipient field cannot be added *minimally*. With ``intent``
    closed, a non-empty recipient list would be a claim that a message is being
    sent inside a scenario whose other two fields are a guest list and a bounded
    date range. The field would therefore either always be empty — unreachable,
    and so not evidence of anything — or be a second statement of ``guests``,
    which is the collapse this round exists to avoid.

    Establishing the representation needs a second intent with its own payload,
    which is a contract amendment rather than an addition.
    """
    assert UnderstandingOutput.model_fields["intent"].annotation == Literal["schedule_meeting"]
    assert get_args(UnderstandingOutput.model_fields["intent"].annotation) == (
        "schedule_meeting",
    )

    for intent in ("send_message", "send_email", "gmail_send", "email"):
        with pytest.raises(ValidationError):
            UnderstandingOutput.model_validate(_scenario(intent=intent))

    # The payload is not a send scene either: a window is required, and a message
    # has none, so there is no way to represent one even with the intent forced.
    without_window = _scenario()
    del without_window["window"]
    with pytest.raises(ValidationError):
        UnderstandingOutput.model_validate(without_window)

    # And the calendar-only object is the only one that survives re-validation,
    # which is what replay does to it, so any recipient field would land here.
    sealed = UnderstandingOutput.model_validate(_scenario())
    reloaded = UnderstandingOutput.model_validate(sealed.model_dump(mode="json"))
    assert reloaded == sealed


# ---------------------------------------------------------------------------
# 3. Memory is already inside the understanding prompt
# ---------------------------------------------------------------------------


async def test_a_memory_address_reaches_the_understanding_model() -> None:
    """The prompt contains the memory's address; the goal contains none.

    ``_memory_notes`` selects ``PERSON`` and ``PREFERENCE`` memory and
    ``_user_datablock`` passes it through verbatim inside ``<preferences>``, so a
    stored address is *textually available* to the understanding model while the
    user's own sentence contains no address at all.

    This is the hazard of licensing condition G, stated as behaviour: a
    recipient extractor added to this stage could read an address out of a
    memory note and seal it, and at the model boundary that is indistinguishable
    from the model having read the goal. So the control cannot be the prompt — it
    has to be a deterministic check that every sealed recipient occurs literally
    in ``<goal>``, a relation this stage does not have.
    """
    gateway, _ = make_gateway()
    runtime = AgentRuntime(gateway=gateway, model=_StubModel(_scenario()))

    async with async_session_factory() as session:
        tenant, user = await make_tenant_user("owner@corp.example")
        await create_memory(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            memory_type=MemoryType.PERSON,
            content=f"Alice Ahmed (colleague) <{ALICE}>",
        )
        task = await make_task(tenant_id=tenant.id, user_id=user.id, state="CREATED")

        notes = await runtime._memory_notes(session, task)
        assert any(ALICE in note for note in notes), "the seeded address must reach the notes"

        model = _StubModel(_scenario())
        sealed = await understand_scenario(
            model, goal=SEND_GOAL, timezone=TZ, memory_notes=notes
        )
        assert sealed.guests == ["Alice"]

        # The address the model was shown, and the goal it was asked about.
        user_content = model.calls[0][1]["content"]
        assert ALICE in user_content
        goal_line = next(
            line for line in user_content.splitlines() if line.startswith("<goal>")
        )
        assert ALICE not in goal_line, "the goal is the only text a recipient may be grounded in"

    # The stage's one deterministic rule is about the calendar's own field, so
    # there is no grounding relation here to extend to a recipient.
    past = datetime.now(ZoneInfo(TZ)).date() - timedelta(days=1)
    with pytest.raises(ValueError, match="starts in the past"):
        await understand_scenario(
            _StubModel(_scenario(window={**_future_window(), "start_date": past.isoformat()})),
            goal=SEND_GOAL,
            timezone=TZ,
        )


async def test_every_sealed_value_is_accepted_on_the_models_word_alone() -> None:
    """Nothing the understanding stage seals is related to the goal.

    A guest who appears nowhere in the goal and a title the user never wrote are
    both accepted, because the only deterministic check concerns the window's
    date. The model is therefore the sole warrant for every sealed value today —
    defensible for a date range and a display name, and not defensible for a
    value the runtime would hand to a provider as an address. Closing that gap is
    what a recipient representation has to do, and it is why the design is a
    grounding check rather than a new field.
    """
    assert not any(char.isdigit() for char in SEND_GOAL)

    sealed = await understand_scenario(
        _StubModel(_scenario(guests=["Nobody The User Never Mentioned"])),
        goal=SEND_GOAL,
        timezone=TZ,
    )

    assert sealed.guests == ["Nobody The User Never Mentioned"]
    assert sealed.window.start_date == date.today() + timedelta(days=3)
    assert sealed.window.title == "KAI"

    # And the whole sealed object survives a database round trip byte for byte —
    # the property any future recipient field would inherit for free.
    assert json.loads(json.dumps(sealed.model_dump(mode="json"))) == sealed.model_dump(
        mode="json"
    )


# ---------------------------------------------------------------------------
# 4. Recipient intent alone would leave the domain half sealed
# ---------------------------------------------------------------------------


def test_recipient_intent_alone_would_leave_the_domain_half_sealed() -> None:
    """A send carries a subject and a body; no sealed source states either.

    ``GmailSendArgs`` requires all three fields, and ``GmailSendCriteria`` states
    only the recipient — refusing ``subject`` and ``body`` for the recorded
    reason that no sealed source states them. So even a perfect recipient-intent
    representation would leave the domain's only other candidate criterion
    unstatable, and a message whose content was invented at execution time could
    be declared fulfilled.

    Recipient intent is therefore a **necessary** precondition for licensing
    ``gmail_send`` and not a sufficient one: a coherent send intent has to seal
    the message's content as intent as well.
    """
    assert set(GmailSendArgs.model_fields) == {"to", "subject", "body"}
    with pytest.raises(ValidationError):
        GmailSendCriteria(required_recipients=frozenset({ALICE}), subject="Q3")
    with pytest.raises(ValidationError):
        GmailSendCriteria(required_recipients=frozenset({ALICE}), body="hello")

    # The two shapes also disagree about cardinality, which is a present contract
    # inconsistency and not only a future integration problem: the criterion is a
    # set while the tool's recipient is a single address, so any criterion naming
    # two addresses is permanently unsatisfiable.
    pair = _criteria(ALICE, "bob@example.com")
    assert len(pair.for_gmail_send().required_recipients) == 2
    for address in (ALICE, "bob@example.com"):
        assert (
            evaluate_gmail_send(FulfillmentInput(criteria=pair, evidence=(_sent(address),)))
            is FulfillmentVerdict.NOT_FULFILLED
        )

    # So a grounded recipient value for this domain would have to be singular, or
    # the tool's shape has to change. Neither happens in this round.
    assert GmailSendArgs(to=ALICE, subject="s", body="b").to == ALICE


# ---------------------------------------------------------------------------
# 5. The licence boundary is unmoved
# ---------------------------------------------------------------------------


def test_gmail_send_is_still_unlicensed_and_the_gate_has_one_entrance() -> None:
    """Nothing this round changed about licensing, and nothing may.

    The chosen representation — a goal-grounded sealed address — is not
    implemented, so the domain's only criterion value is still a resolution
    projection and the profile stays out of the licence set. The set is a closed
    literal checked at one place — it now also holds the local, provider-free
    ``task_preparation`` profile added in Stage 2E — so a second path to live
    sealing cannot appear without an edit to the single function that grants it.
    """
    assert frozenset({"calendar_meeting", "task_preparation"}) == LIVE_SEALABLE_PROFILES
    assert "gmail_send" in get_args(FulfillmentProfile)

    # The set is a closed literal and the gate is a single membership test, so a
    # second path to live sealing cannot appear without editing the one function
    # that writes the row. Nothing else consults the set.
    text = Path(task_service_module.__file__ or "").read_text(encoding="utf-8")
    assert text.count("not in LIVE_SEALABLE_PROFILES") == 1
    assert "LIVE_SEALABLE_PROFILES: frozenset" in text

    # The calendar licence still rests on a value the understanding stage sealed
    # and the calendar evaluator decides on, so this round changed nothing there.
    sealed = UnderstandingOutput.model_validate(_scenario())
    outcome = _fulfillment_outcome(
        _TaskRow(),
        scenario=sealed,
        identities=[{"name": "Alice", "email": ALICE}],
        evidence=[],
    )
    assert outcome.profile == "calendar_meeting"
    assert (
        calendar_meeting_criteria(
            profile="calendar_meeting",
            window=sealed.window,
            identities=[{"name": "Alice", "email": ALICE}],
        )
        .for_calendar_meeting()
        .required_attendees
        == frozenset({ALICE})
    )


def test_the_public_projection_would_widen_and_the_program_identity_would_not() -> None:
    """Adding a field is additive to the API and invisible to KAI-IR.

    The sealed scenario is returned to clients inside ``RuntimeTaskResponse``, so
    a new field would appear there. The IR program is the other half of the cost
    picture and is unaffected: ``IntentNode`` reproduces the contract verbatim and
    deliberately keeps the scenario out, and the canonical fingerprint is taken
    over the plan rather than over the sealed understanding. The representation's
    cost is a contract amendment, not an identity change.
    """
    assert RuntimeTaskResponse.model_fields["scenario"].annotation == (UnderstandingOutput | None)

    # The program cannot carry the scenario, so a sealed value cannot reach it:
    # the node reproduces the contract verbatim and refuses anything else.
    node = IntentNode(goal=SEND_GOAL, success_criteria=["a message was sent"])
    assert set(type(node).model_fields) == {"goal", "success_criteria", "entry"}
    with pytest.raises(ValidationError):
        IntentNode(
            goal=SEND_GOAL,
            success_criteria=["a message was sent"],
            scenario={"recipients": [ALICE]},
        )

    # And the fingerprint is a function of the contract alone, so the same
    # contract always identifies the same program whatever the runtime seals.
    program = {
        "intent": {"goal": SEND_GOAL, "success_criteria": ["a"], "entry": True},
        "steps": [{"step_id": "s1", "order": 1, "capability": "send", "purpose": "send"}],
    }
    assert canonical_fingerprint(program) == canonical_fingerprint(dict(program))


# ---------------------------------------------------------------------------
# 6. The chosen representation, and why each part is load-bearing
# ---------------------------------------------------------------------------


def test_the_representation_is_a_grounded_address_not_a_resolved_contact() -> None:
    """The design, stated as the properties a value would have to have.

    No value exists yet, so what is pinned here are the four properties that
    decide the design, each with the case that rules the alternative out:

    * **grounded** — it must occur literally in the goal, or it is not the
      user's own sentence. Rule-out: ``Alice`` → ``alice@example.com``.
    * **an address** — or the evaluator has no join key for it. Rule-out: a
      name-shaped criterion, which is unconditionally unsatisfied.
    * **not a contact** — a contact match is still required before the send, so
      the sealed value is a claim the provider has to satisfy, not one it
      supplies. Rule-out: treating ``CONTACT_MATCH`` as the source.
    * **not the account** — a default recipient is the invention the resolver's
      provider branches would smuggle in, and those branches are unreachable
      today because the runtime builds the query itself.
    """
    # The resolver's account-address branches cannot be the source: every query
    # the runtime builds carries a name or an email and nothing else.
    for query in (IdentityQuery(name="Alice"), IdentityQuery(email=ALICE)):
        assert set(query.model_dump(exclude_none=True)) <= {"name", "email"}

    # Grounding is decidable without a model — it is a substring test against the
    # goal the stage already receives — which is what makes it a warrant and not a
    # request. The ordinary goal grounds nothing; a goal that states the address
    # grounds exactly that address, and never the memory-supplied one.
    assert ALICE not in SEND_GOAL
    assert ALICE in f"send the plan to {ALICE} before friday"
