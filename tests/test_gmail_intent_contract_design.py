"""The complete sealed user-intent model a ``gmail_send`` would need, and the two
places where the repository cannot yet supply one.

The previous round fixed the *recipient* half: a sealed value has to be an address,
because that is the evaluator's only join key, and it has to occur literally in the
user's goal, because a ``PERSON`` memory note carrying the same address is already
inside the understanding prompt (``test_sealed_recipient_intent_design.py``). What
that round did not establish is the *whole* contract — whether recipient, subject
and body together form the minimum, and where the sealing boundary actually falls.

This round asks the remaining question, and the answer is a finding rather than a
design:

    **The license is sealed before the value that could justify it exists.**

``build_slice_contract`` runs at submission and stamps ``fulfillment_profile`` on the
task row; the understanding model is not called until a later trigger, and grounding
happens after that. So a recipient value is not merely unrepresented — it does not
exist at the moment the repository decides whether a send may be sealed at all. That
is a temporal ordering property, and no schema change removes it.

Three further facts close off the alternative of sealing content instead:

* ``_intents_for`` is the sole author of tool arguments, and it is a closed
  enumeration of the four calendar capabilities. There is no model-authored argument
  path — ``PlanStep.inputs`` is ``list[str]`` and is copied into the IR, never read as
  arguments. So a driven send's ``subject`` and ``body`` would have to be projected
  from the sealed scenario, and the sealed scenario has neither field.
* ``verify_send`` compares the recipient and the subject against the **request**, and
  does not compare the body at all. So a body criterion would have no evidence to read
  even if a sealed body existed.
* Grounding is a *snapshot*, not a recomputation, and the snapshot is the only input to
  the domain's sole criterion while its own ``status`` is discarded by the projection.
  A row that recorded a failed grounding therefore still yields a required recipient.

None of these is fixed by adding a field, and two of them — where the sealing boundary
sits, and what may author a message body — are policy decisions the repository does not
settle. So this file changes no production code, and ``gmail_send`` stays unlicensed.
It pins the premises a round that *does* settle them has to move deliberately.
"""

from __future__ import annotations

import inspect
import textwrap
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from kai.gmail.models import EmailAddress, GmailMessage
from kai.gmail.service import verify_send
from kai.identity import IdentityQuery, IdentityResolver, IdentityStatus
from kai.planner.schemas import PlanStep
from kai.runtime.fulfillment import (
    FulfillmentInput,
    FulfillmentVerdict,
    evaluate_gmail_send,
    gmail_send_criteria,
)
from kai.runtime.schemas import UnderstandingOutput
from kai.runtime.send_evidence import qualifying_sends
from kai.runtime.service import AgentRuntime, RuntimeFatal, _fulfillment_outcome
from kai.runtime.translator import (
    SLICE_ALLOWED_TOOLS,
    build_slice_contract,
    capability_to_tool,
)
from kai.runtime.understanding import UNDERSTANDING_SYSTEM_PROMPT
from kai.tasks.service import LIVE_SEALABLE_PROFILES
from kai.tools.schemas import GmailSendArgs
from tool_harness import (
    GoogleBackend,
    contact_payload,
    insert_account,
    make_context,
    make_gateway,
    make_task,
    make_tenant_user,
    session_scope,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALICE: Final = "alice@example.com"
OWNER: Final = "owner@corp.example"
TZ: Final = "UTC"

SEND_GOAL: Final = "email Alice about the quarterly plan"


def _source(node: Any) -> str:
    return textwrap.dedent(inspect.getsource(node))


def _future_window() -> dict[str, Any]:
    start = date.today() + timedelta(days=3)
    return {
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=1)).isoformat(),
        "earliest_time": "09:00",
        "title": "KAI",
    }


def _scenario() -> dict[str, Any]:
    return {
        "intent": "schedule_meeting",
        "guests": ["Alice"],
        "window": _future_window(),
    }


def _sent(to: str, *, subject: str = "Quarterly plan", body: str = "hello") -> dict[str, Any]:
    """One qualifying, verified, provider-identified ``gmail.send`` attempt."""
    return {
        "tool": "gmail.send",
        "outcome": "EXECUTION_SUCCEEDED",
        "ok": True,
        "verified": True,
        "args": {"to": to, "subject": subject, "body": body},
        "evidence": {"message_id": "m1"},
    }


def _criteria(*rows: tuple[str, str, str]):
    """Sealed criteria built from ``identities`` rows of (name, email, status)."""
    return gmail_send_criteria(
        profile="gmail_send",
        identities=[
            {"name": name, "email": email, "status": status, "confidence": 0.5}
            for name, email, status in rows
        ],
    )


def _verdict(criteria: Any, *records: dict[str, Any]) -> FulfillmentVerdict:
    return evaluate_gmail_send(FulfillmentInput(criteria=criteria, evidence=records))


class _StubModel:
    """A model that returns exactly what the test hands it."""

    model_id = "stub-understanding-v0"

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response

    async def generate_plan(self, *, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return self.response


def _readback(**overrides: Any) -> GmailMessage:
    """A provider read-back of a message we claim to have sent."""
    base: dict[str, Any] = {
        "message_id": "m1",
        "thread_id": "t1",
        "subject": "Quarterly plan",
        "from_addr": EmailAddress(name="Owner", email=OWNER),
        "to": (EmailAddress(name="Alice", email=ALICE),),
        "date": "Mon, 01 Jan 2035 09:00:00 +0000",
        "snippet": "hi",
        "body_text": "hello",
        "label_ids": ("SENT",),
    }
    base.update(overrides)
    return GmailMessage(**base)


# ---------------------------------------------------------------------------
# 1. The runtime is the only author of tool arguments, and it has no send branch
# ---------------------------------------------------------------------------


async def test_a_send_capability_cannot_be_translated_into_a_tool_call() -> None:
    """There is one argument author, it is deterministic, and it has no gmail branch.

    ``PlanStep`` is a capability plus prose: ``inputs`` is ``list[str]`` and is copied
    into the IR node, never read as arguments. Every argument the gateway ever sees is
    therefore built by ``_intents_for`` from the sealed scenario and the resolved row —
    so the question "where would a send's content come from?" has exactly one possible
    answer, and that answer is a closed enumeration of four calendar capabilities.
    """
    # The plan really does carry no arguments.
    assert "arguments" not in PlanStep.model_fields
    assert PlanStep.model_fields["inputs"].annotation == list[str]

    # The capability table has no send capability, so a send capability is untranslatable
    # rather than merely unused.
    assert capability_to_tool("send_message") is None
    assert "gmail.send" not in SLICE_ALLOWED_TOOLS
    assert not any("gmail" in tool for tool in SLICE_ALLOWED_TOOLS)

    # And the argument author refuses it, by name, on the live path.
    gateway, _ = make_gateway()
    runtime = AgentRuntime(gateway=gateway, model=_StubModel(_scenario()))
    with pytest.raises(RuntimeFatal, match="not supported by the agent runtime slice"):
        runtime._intents_for(
            "send_message",
            scenario=UnderstandingOutput.model_validate(_scenario()),
            identities=[{"name": "Alice", "email": ALICE, "status": "CONTACT_MATCH"}],
            collected=[],
            created_event_id=None,
            timezone=TZ,
        )


async def test_the_calendar_argument_builder_takes_only_sealed_inputs() -> None:
    """The contrast that makes the gap visible: every field has a sealed origin.

    ``create_intent_args`` receives the title from the sealed scenario and the
    addresses from the resolved row, and nothing else — so a calendar request is
    traceable field by field back to durable state. The send domain has no builder to
    compare against, and ``GmailSendArgs`` requires ``subject`` and ``body``, which the
    sealed scenario does not contain. A driven send's content would therefore have to be
    a constant, in the same way ``MeetingWindow.title`` falls back to a default.
    """
    assert set(UnderstandingOutput.model_fields) == {"intent", "guests", "window"}
    assert set(GmailSendArgs.model_fields) == {"to", "subject", "body"}

    # The title is the one content-shaped value the understanding stage seals, and it
    # reaches the provider as an execution argument without ever being a criterion.
    from kai.runtime.translator import create_intent_args

    args = create_intent_args(
        slot=(date.today() + timedelta(days=3), date(2020, 1, 1), date(2020, 1, 1)),
        title="Q3 review",
        timezone=TZ,
        attendee_emails=[ALICE],
    )
    assert args["title"] == "Q3 review"
    assert args["attendees"] == [ALICE]
    # The domain's own criteria never read the title back.
    assert "title" not in {
        field for field in GmailSendArgs.model_fields if field in {"to", "subject", "body"}
    }


# ---------------------------------------------------------------------------
# 2. The provider attests the subject against the request, and never the body
# ---------------------------------------------------------------------------


def test_the_body_is_not_attested_anywhere_on_the_send_path() -> None:
    """``verify_send`` checks six fields and the body is not one of them.

    Recipient and subject are compared against the **request** — so ``verified=True``
    means "the provider echoed what we asked it to send", and says nothing about the
    Intent. The body is not compared at all: a read-back whose text is entirely
    different still verifies clean. So a body criterion would have no evidence to read
    even if a sealed body existed, which is a stronger obstacle than the missing
    sealed source and is why body authority is a policy question rather than a field.
    """
    clean = _readback()
    assert verify_send(
        clean, expected_id="m1", sender_email=OWNER, to=ALICE, subject="Quarterly plan"
    ) == ()

    # Every body the provider can return is accepted, including an empty one.
    for body_text in ("", "a completely different body", "x" * 100_000):
        assert verify_send(
            _readback(body_text=body_text),
            expected_id="m1",
            sender_email=OWNER,
            to=ALICE,
            subject="Quarterly plan",
        ) == ()

    # The fields that *are* checked, each of which does fail.
    for override, expected in (
        ({"to": (EmailAddress(name="Zed", email="zed@example.com"),)}, "recipient"),
        ({"subject": "Different subject"}, "subject"),
        ({"label_ids": ()}, "label_ids"),
        ({"message_id": "m2"}, "message_id"),
    ):
        mismatches = verify_send(
            _readback(**override),
            expected_id="m1",
            sender_email=OWNER,
            to=ALICE,
            subject="Quarterly plan",
        )
        assert [m.field for m in mismatches] == [expected]

    # And the body is not readable from the record the evaluator sees either, so no
    # criterion could compare against it.
    witness = qualifying_sends([_sent(ALICE, body="the real body")])[0]
    assert "body" in witness.arguments


# ---------------------------------------------------------------------------
# 3. Grounding: what the resolver can and cannot decide
# ---------------------------------------------------------------------------


async def test_a_name_can_never_ground_and_an_address_gates_on_an_exact_match() -> None:
    """The only positive outcome reachable for a recipient is ``CONTACT_MATCH``.

    ``_exact_claims`` reads email and phone and never the name, so a name match can
    only make the answer CONFLICTING or leave it UNKNOWN. That is why the previous
    round's grounded-address shape is not merely preferable but *necessary*: a name the
    user wrote has no deterministic path to an address at all, so the user has to state
    the address, and the resolver's job is to confirm it is a real contact.
    """
    async with session_scope() as session:
        tenant, user = await make_tenant_user(OWNER)
        account = await insert_account(
            tenant_id=tenant.id, user_id=user.id, email=OWNER, sub="sub-1"
        )
        task = await make_task(tenant_id=tenant.id, user_id=user.id)
        one_contact = [
            contact_payload(resource_name="people/1", display_name="Alice", emails=(ALICE,))
        ]

        def _query() -> IdentityQuery:
            return IdentityQuery(name="Alice")

        by_name = await IdentityResolver().resolve(
            query=_query(),
            context=make_context(
                session=session, task=task, backend=_backend(one_contact),
                account_id=account.id, identity=_query(),
            ),
        )
        assert by_name.status is IdentityStatus.UNKNOWN
        assert by_name.email is None

        by_email_query = IdentityQuery(email=ALICE)
        by_email = await IdentityResolver().resolve(
            query=by_email_query,
            context=make_context(
                session=session, task=task, backend=_backend(one_contact),
                account_id=account.id, identity=by_email_query,
            ),
        )
        assert by_email.status is IdentityStatus.CONTACT_MATCH
        assert by_email.email == ALICE


def _backend(connections: list[dict[str, Any]]) -> GoogleBackend:
    backend = GoogleBackend()
    backend.contacts_connections = connections
    return backend


async def test_the_outcome_vocabulary_now_distinguishes_grounded_from_absent() -> None:
    """The collapse this design review found is gone, and the vocabulary is sound.

    The original finding: the outcome vocabulary has no ``AMBIGUOUS`` member, and
    that omission used to be *visible*. Two distinct contacts carrying the same
    address are CONFLICTING. But a single contact matched on *both* email and phone
    — an ordinary contact, queried with both identifiers — produced two exact
    claims, failed the ``len(exact_claims) == 1`` test, and also fell through to
    UNKNOWN. So "I found exactly one contact", "I found two ways to match one
    contact" and "I found nobody" all read UNKNOWN, while the value the caller
    wanted stayed populated on the row. That was the concrete reason the design
    could not name a grounding rule: the VALID / AMBIGUOUS / UNRESOLVED
    distinction had no representation to project onto.

    The root cause was never the vocabulary. It was a predicate counting claims
    where the conflict rule counted entities. With that corrected the three cases
    separate cleanly on states that already existed, and the design question this
    test was blocking is answerable. No ``AMBIGUOUS`` member is still needed and
    none was added: CONFLICTING already means "more than one entity", and UNKNOWN
    already means "no entity".

    The finding is kept because the corrected branch is still unreachable from any
    product query — every ``IdentityQuery`` the runtime builds is email-only or
    name-only — so this guard is what keeps a latent fix from being reverted.
    """
    assert not any("AMBIGUOUS" in status.value for status in IdentityStatus)
    assert {status.value for status in IdentityStatus} == {
        "CONTACT_MATCH",
        "PROVIDER_VERIFIED",
        "CHANNEL_VERIFIED",
        "UNKNOWN",
        "CONFLICTING",
    }

    async with session_scope() as session:
        tenant, user = await make_tenant_user(OWNER)
        account = await insert_account(
            tenant_id=tenant.id, user_id=user.id, email=OWNER, sub="sub-1"
        )
        task = await make_task(tenant_id=tenant.id, user_id=user.id)

        async def _resolve(connections: list[dict[str, Any]], query: IdentityQuery):
            return await IdentityResolver().resolve(
                query=query,
                context=make_context(
                    session=session, task=task, backend=_backend(connections),
                    account_id=account.id, identity=query,
                ),
            )

        one = contact_payload(resource_name="people/1", display_name="Alice", emails=(ALICE,))

        # Two distinct contacts holding the same address: a gate failure.
        colliding = await _resolve(
            [
                one,
                contact_payload(
                    resource_name="people/2", display_name="Alice w", emails=(ALICE,)
                ),
            ],
            IdentityQuery(email=ALICE),
        )
        assert colliding.status is IdentityStatus.CONFLICTING
        assert colliding.contact_id is None

        # One contact matched on both identifiers: grounded, because there is one
        # entity. This used to be UNKNOWN. The value the caller wanted was already
        # populated then, and the status now agrees with it.
        both = contact_payload(
            resource_name="people/1", display_name="Alice", emails=(ALICE,), phones=("+15550100",)
        )
        matched_twice = await _resolve(
            [both], IdentityQuery(email=ALICE, phone="+15550100")
        )
        assert matched_twice.status is IdentityStatus.CONTACT_MATCH
        assert matched_twice.email == ALICE
        assert matched_twice.contact_id == "people/1"

        # And no contact at all is still UNKNOWN, distinct from both of the above.
        absent = await _resolve([both], IdentityQuery(email="zed@nowhere.example"))
        assert absent.status is IdentityStatus.UNKNOWN
        assert absent.email is None

        # The three cases the contract needs are now three different states:
        # grounded, contested, unresolved. No new member was required.
        assert {
            matched_twice.status, colliding.status, absent.status
        } == {
            IdentityStatus.CONTACT_MATCH,
            IdentityStatus.CONFLICTING,
            IdentityStatus.UNKNOWN,
        }

        # The address is still readable from a result whose status is not positive,
        # which is why the runtime guards on status at write time rather than on the
        # email. A populated address is a payload, never an authority.
        assert colliding.email == ALICE
        assert _criteria(
            ("Alice", colliding.email, colliding.status.value)
        ).for_gmail_send().required_recipients == frozenset({ALICE})


# ---------------------------------------------------------------------------
# 4. The sealing boundary: the licence precedes the value that would justify it
# ---------------------------------------------------------------------------


def test_the_profile_is_sealed_at_submission_before_any_intent_exists() -> None:
    """The ordering is the finding: license first, value later.

    ``build_slice_contract`` takes only the goal and stamps ``calendar_meeting`` on the
    contract; ``create_task`` writes it to the row and refuses anything outside
    ``LIVE_SEALABLE_PROFILES``. The understanding model is not called until a later
    trigger, and grounding runs after that. So there is no point in the lifecycle at
    which a recipient value exists and the repository could have weighed it before
    granting the licence.

    This is why the previous round's two prerequisites are necessary but not
    sufficient: a grounded address and a subject/body source do not by themselves move
    the sealing boundary, and no schema change moves it either.
    """
    contract = build_slice_contract(SEND_GOAL)
    assert contract.fulfillment_profile == "calendar_meeting"

    # The builder is scenario-agnostic by construction: it never receives a scenario,
    # so it cannot read a recipient even if one were sealed.
    assert "scenario" not in inspect.signature(build_slice_contract).parameters
    assert not {"recipients", "recipient", "subject", "body"} & set(
        type(contract).model_fields
    )
    assert set(type(contract).model_fields) == {
        "goal",
        "success_criteria",
        "fulfillment_profile",
        "allowed_tools",
        "risk_policy",
        "deadline",
        "max_cost",
        "max_steps",
        "verification_method",
        "fallback",
    }

    # The understanding prompt knows about neither, so a send could not be decoded
    # into this shape even by accident.
    prompt = UNDERSTANDING_SYSTEM_PROMPT
    assert 'intent is always "schedule_meeting"' in prompt
    for absent in ("recipient", "subject", "body", "send", "email"):
        assert absent not in prompt.lower()


# ---------------------------------------------------------------------------
# 5. Replay: a snapshot with no schema, whose verdict the projection discards
# ---------------------------------------------------------------------------


def test_replay_revalidates_the_scenario_and_not_the_identities() -> None:
    """Two durable columns hold the two inputs, and only one has a schema.

    ``scenario`` is re-validated by ``UnderstandingOutput`` on every trigger, so a
    tampered scenario is refused. ``identities`` is read as ``list(row.identities)`` and
    has no model at all, so a malformed row surfaces as an untyped ``AttributeError``
    from inside the criteria builder rather than as a refusal. The asymmetry is what
    makes the next pin possible.
    """
    sealed = UnderstandingOutput.model_validate(_scenario())
    assert UnderstandingOutput.model_validate(sealed.model_dump(mode="json")) == sealed

    tampered = sealed.model_dump(mode="json")
    tampered["guests"] = []
    with pytest.raises(ValidationError):
        UnderstandingOutput.model_validate(tampered)

    # The identities side has no such guard: a row that is not a list of mappings is
    # not refused, it crashes the projection.
    with pytest.raises(AttributeError):
        gmail_send_criteria(profile="gmail_send", identities=["not-a-mapping"])  # type: ignore[list-item]


def test_a_failed_grounding_verdict_becomes_a_required_recipient_on_replay() -> None:
    """Persistence launders a grounding failure into a sealed criterion.

    The runtime refuses to write a row whose status is not positive, but that guard is
    at write time only. The criteria builder reads ``email`` and ignores ``status``, and
    the row is re-read unvalidated on every replay — so a row recording CONFLICTING or
    UNKNOWN still produces a required recipient, and a send to that address is decided
    FULFILLED.

    This is the concrete form of the invariant the round is about: persistence does not
    create authority. Here it does not merely fail to create it, it destroys the record
    that the grounding verdict was *not* satisfied.
    """
    for failed in (IdentityStatus.CONFLICTING, IdentityStatus.UNKNOWN):
        criteria = _criteria(("Alice", ALICE, failed.value))
        assert criteria.for_gmail_send().required_recipients == frozenset({ALICE})
        assert _verdict(criteria, _sent(ALICE)) is FulfillmentVerdict.FULFILLED

    # The projection is what drops the verdict, and it is the only place it is dropped.
    source = _source(gmail_send_criteria)
    assert '"email"' in source
    assert "status" not in source


def test_grounding_is_a_snapshot_rather_than_a_recomputation() -> None:
    """Grounding runs once, so "recomputation must not become intent" is vacuous today.

    The stage is guarded by ``if not row.identities``, so the resolver is never asked
    the question again on a later trigger. Nothing can become new intent by being
    recomputed, because nothing is recomputed — and the price is that a contact which
    changed after the original execution is invisible to the replay, with nothing
    recording that it was ever consulted again.

    So the round's premise holds only trivially, and the real exposure is the one the
    previous pin shows: the snapshot is unvalidated and its verdict is discarded.
    """
    source = _source(AgentRuntime._drive)
    assert "if not row.identities:" in source
    assert source.count("_resolve_guest_identities") == 1

    # The identity stage never revalidates either, and reads the row as a bare list.
    execute = _source(AgentRuntime._execute)
    assert "identities: list[dict[str, Any]] = list(row.identities)" in execute


# ---------------------------------------------------------------------------
# 6. The invariants: persistence, determinism and provider output grant nothing
# ---------------------------------------------------------------------------


def test_no_provider_output_can_become_intent() -> None:
    """The only provider field the domain reads is a message id, and it decides nothing.

    A send the provider addressed somewhere the sealed criteria did not name is refused,
    however well verified the attempt was. The provider's own ``to`` and ``subject`` on
    the read-back are not in the record the evaluator sees at all — only the request's
    arguments are — so a provider-chosen recipient has no path into a criterion.
    """
    criteria = _criteria(("Alice", ALICE, IdentityStatus.CONTACT_MATCH.value))

    assert _verdict(criteria, _sent(ALICE)) is FulfillmentVerdict.FULFILLED
    for other in ("zed@example.com", "attacker@evil.example", "alice@example.com.evil.test"):
        assert _verdict(criteria, _sent(other)) is FulfillmentVerdict.NOT_FULFILLED

    # Address comparison is case-folded on both sides, so a differently-cased spelling
    # of the same mailbox is the same recipient rather than a different one.
    assert _verdict(criteria, _sent(ALICE.upper())) is FulfillmentVerdict.FULFILLED

    # The record's provider container is read for one key, and that key is an identity
    # rather than a claim about the intent.
    witness = qualifying_sends([_sent(ALICE)])[0]
    assert witness.provider_message_id == "m1"
    assert set(witness.arguments) == {"to", "subject", "body"}


def test_determinism_and_persistence_change_no_verdict() -> None:
    """The verdict is a pure function of the sealed criteria and the evidence set.

    Re-deriving the criteria from the same row, in any order, and re-deriving the
    verdict from a permuted log both give the same answer. That is the property the
    design must keep when a real recipient intent is added: whatever grounds the
    address, re-running the derivation may not move the answer.
    """
    rows = [("Alice", ALICE, "CONTACT_MATCH"), ("Zed", "zed@example.com", "CONTACT_MATCH")]
    first = _criteria(*rows)
    second = _criteria(*rows)
    assert first == second
    assert _criteria(*reversed(rows)) == first

    send, other = _sent(ALICE), _sent("zed@example.com")
    forward, backward = (send, other), (other, send)
    # Permuting the log does not move the answer, and the answer is NOT_FULFILLED in
    # both orders because the criterion is a set of two addresses while a send can
    # carry only one. That is the cardinality mismatch, and it is order-independent.
    assert _verdict(first, *forward) is _verdict(first, *backward) is (
        FulfillmentVerdict.NOT_FULFILLED
    )

    # With one sealed recipient the same property holds against a satisfying verdict.
    single = _criteria(("Alice", ALICE, "CONTACT_MATCH"))
    assert _verdict(single, *forward) is _verdict(single, *backward) is (
        FulfillmentVerdict.FULFILLED
    )


# ---------------------------------------------------------------------------
# 7. Nothing the round needed was a licence, and the calendar path is untouched
# ---------------------------------------------------------------------------


def test_calendar_semantics_are_unchanged_and_the_gate_stays_single() -> None:
    """The calendar branch still reads the sealed window; gmail still reads no scenario.

    The two domains take different sealed inputs and that is deliberate, so the
    gmail branch's refusal to read ``scenario`` is the standing reason a recipient
    intent would have to be added rather than reused. Neither branch nor the single
    licence check changed, and the profile set is unchanged.
    """
    sealed = UnderstandingOutput.model_validate(_scenario())
    identities = [{"name": "Alice", "email": ALICE, "status": "CONTACT_MATCH", "confidence": 0.5}]

    class _Row:
        fulfillment_profile: str = "calendar_meeting"

    calendar = _fulfillment_outcome(
        _Row(), scenario=sealed, identities=identities, evidence=[]
    )
    assert calendar.profile == "calendar_meeting"
    assert calendar.version == "calendar_meeting:v1"
    assert calendar.verdict is FulfillmentVerdict.NOT_FULFILLED

    class _GmailRow:
        fulfillment_profile: str = "gmail_send"

    gmail = _fulfillment_outcome(
        _GmailRow(), scenario=sealed, identities=identities, evidence=[_sent(ALICE)]
    )
    assert gmail.profile == "gmail_send"
    assert gmail.verdict is FulfillmentVerdict.FULFILLED

    # The licence is still one closed literal checked in one place, and gmail is still
    # outside it. Nothing in this round moved that, and a second path to live sealing
    # would need an edit to the single function that writes the row.
    assert frozenset({"calendar_meeting"}) == LIVE_SEALABLE_PROFILES

    from kai.tasks import service as task_service

    source = Path(task_service.__file__ or "").read_text(encoding="utf-8")
    assert source.count("not in LIVE_SEALABLE_PROFILES") == 1


def test_the_semantic_minimum_is_not_yet_determinable() -> None:
    """The round's own decision gate, stated as a property rather than as prose.

    Implementation requires all of: a recipient intent representation, a grounding
    rule, a subject authority, a body authority, a sealing boundary, replay behaviour,
    and a fulfillment projection. Four are determined by the repository and three are
    not, and the three that are not are the ones a field cannot supply:

    * **subject authority** — no sealed source, no builder, and the provider compares
      it only to the request, so whether a subject may be synthesised from the goal is
      an unmade policy decision;
    * **body authority** — the same, and worse, because the body is not compared by
      the provider at all, so a sealed body would have no evidence to decide on;
    * **the sealing boundary** — the licence is granted at submission and the value
      exists later, so where the boundary should fall is a decision, not an amendment.

    Recipient representation, replay behaviour and the projection *are* settled: an
    address stated literally in the goal, a snapshot that is never recomputed, and
    ``required_recipients <= {args["to"]}``. The gate stays shut on the other three.
    """
    # Determined: the join key, the grounding that is reachable, the projection.
    assert sorted(qualifying_sends([_sent(ALICE)])[0].arguments) == ["body", "subject", "to"]
    assert IdentityStatus.CONTACT_MATCH.value in {s.value for s in IdentityStatus}

    # Undetermined: nothing in the repository states a subject or a body authority.
    from kai.runtime import schemas as runtime_schemas

    understanding_fields = set(runtime_schemas.UnderstandingOutput.model_fields)
    assert not understanding_fields & {"subject", "body", "recipients", "recipient"}

    # And the profile that would need them is still unlicensed, which is the gate's
    # own record of the three open questions.
    assert "gmail_send" not in LIVE_SEALABLE_PROFILES
    assert frozenset({"calendar_meeting"}) == LIVE_SEALABLE_PROFILES
