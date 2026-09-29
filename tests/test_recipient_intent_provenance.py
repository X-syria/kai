"""Recipient intent provenance for ``gmail_send``: guests → required_recipients.

The earlier round generalised one fulfillment mechanism and left the meaning
unproven. This round asks one narrower question about a single value: for
``GmailSendCriteria.required_recipients``, *what is the authority?* The question
is asked by tracing the value end to end, not by reading field names:

```text
user goal
    -> UnderstandingOutput{guests}          MODEL INTERPRETATION, sealed in .scenario
    -> memory_email_hint(name=guest)        PERSON memory content, regex-first email
    -> IdentityQuery(email=hint)            RESOLVER OUTPUT (query), gate only
    -> contacts exact-match                 VERIFIED FACT (existence of the address)
    -> row.identities[email]                JOIN, sealed in the runtime row
    -> required_recipients                  SUCCESS CRITERION
    -> evaluator vs durable log             EVIDENCE / REQUEST DATA
```

Four results are pinned here that the code does not support reading as intent:

1. **There is no sealed recipient intent anywhere.** ``UnderstandingOutput`` is
   ``{intent, guests, window}`` with ``extra="forbid"``; the prompt never says
   recipient/email/send; no durable column holds an address the user asked for.
2. **On the only live producer the address is memory content, not resolver
   output.** ``_exact_claims`` builds its claim value from the *query* email, so
   ``resolved.email`` is the normalized PERSON-memory hint and the contact is only
   an existence gate. A name alone can never produce an address: name-only queries
   have no exact claim, so they resolve UNKNOWN and the runtime raises
   ``RuntimeFatal`` before any tool runs.
3. **A multi-address criterion is structurally unsatisfiable.** ``required_recipients``
   is a ``frozenset`` over up to five guests, while ``GmailSendArgs.to`` is a
   single ``EmailStr``, so the evaluator's recipient set is always a singleton and
   any two distinct addresses are permanently NOT_FULFILLED.
4. **Identity status and confidence carry no recipient meaning.** They are
   qualification-only, the criteria read ``email`` alone, and on the live path
   only ``CONTACT_MATCH`` is reachable at all.

The name change between rounds is a rename of the *claim*, not a reversal of it:
this file does not argue for adding a recipient field, it pins that none exists.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from typing import Any, Final

import pytest
from pydantic import ValidationError

from kai.identity import IdentityQuery, IdentityResolver, IdentityStatus
from kai.runtime.fulfillment import (
    FulfillmentInput,
    FulfillmentVerdict,
    _meeting_satisfies_criteria,
    _send_satisfies_criteria,
    calendar_meeting_criteria,
    gmail_send_criteria,
)
from kai.runtime.models import RuntimeExecution
from kai.runtime.schemas import RuntimeIdentity, UnderstandingOutput
from kai.runtime.service import AgentRuntime, _fulfillment_outcome
from kai.runtime.understanding import UNDERSTANDING_SYSTEM_PROMPT
from kai.tasks.service import LIVE_SEALABLE_PROFILES
from kai.tools.schemas import GmailSendArgs
from tool_harness import (
    GoogleBackend,
    contact_payload,
    insert_account,
    make_context,
    make_task,
    make_tenant_user,
    session_scope,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALICE: Final = "alice@example.com"


def _identities(*rows: tuple[str, str]) -> list[dict[str, Any]]:
    """Sealed ``row.identities`` shape exactly as the runtime writes it."""
    return [
        {
            "name": name,
            "email": email,
            "status": IdentityStatus.CONTACT_MATCH.value,
            "confidence": 0.5,
        }
        for name, email in rows
    ]


def _required(*rows: tuple[str, str]) -> frozenset[str]:
    return (
        gmail_send_criteria(profile="gmail_send", identities=_identities(*rows))
        .for_gmail_send()
        .required_recipients
    )


def _required_from(rows: list[dict[str, Any]]) -> frozenset[str]:
    return (
        gmail_send_criteria(profile="gmail_send", identities=rows)
        .for_gmail_send()
        .required_recipients
    )


def _send(to: str, *, record: dict[str, Any] | None = None) -> dict[str, Any]:
    """A qualifying durable log record, varying only in the request's address."""
    base: dict[str, Any] = {
        "tool": "gmail.send",
        "outcome": "EXECUTION_SUCCEEDED",
        "ok": True,
        "verified": True,
        "args": {"to": to, "subject": "Quarterly plan", "body": "hello"},
        "evidence": {"message_id": "m1"},
    }
    if record is not None:
        base.update(record)
    return base


def _verdict(criteria: Any, *records: dict[str, Any]) -> FulfillmentVerdict:
    from kai.runtime.fulfillment import evaluate_gmail_send

    return evaluate_gmail_send(FulfillmentInput(criteria=criteria, evidence=records))


def _source(node: Any) -> str:
    return textwrap.dedent(inspect.getsource(node))


# ---------------------------------------------------------------------------
# 1. Provenance graph: source -> transformation -> stored -> consumer
# ---------------------------------------------------------------------------


def test_the_provenance_graph_has_no_user_intent_above_the_address() -> None:
    """Walk the chain backwards from the criterion and name every hop.

    ``required_recipients`` is built from ``identities[*].email`` and nothing
    else, so the question of intent is entirely a question about that column.
    Every hop between the user's words and the value is pinned here, and the
    first hop is a model reading of names — there is no hop in which the user
    states an address.
    """
    import kai.runtime.fulfillment as F

    source = _source(F.gmail_send_criteria)
    # Consumer: the criterion's only input is the durable identity rows.
    assert "identities" in source
    assert "scenario" not in source
    assert "goal" not in source
    assert "success_criteria" not in source
    assert "identities" in {c.name for c in RuntimeExecution.__table__.columns}

    # Sealed understanding: names only, and no field may be added to say more.
    assert set(UnderstandingOutput.model_fields) == {"intent", "guests", "window"}
    with pytest.raises(ValidationError):
        UnderstandingOutput.model_validate(
            {
                "intent": "schedule_meeting",
                "guests": ["Alice"],
                "window": {
                    "start_date": "2026-06-24",
                    "end_date": "2026-06-25",
                    "earliest_time": "09:00",
                    "title": "KAI",
                },
                "recipients": [ALICE],
            }
        )

    # The model was never asked for one either.
    lowered = UNDERSTANDING_SYSTEM_PROMPT.lower()
    for forbidden in ("recipient", "email", "send", "gmail"):
        assert forbidden not in lowered

    # So the chain above the column is: goal -> model names -> memory -> gate.
    assert "recipients" not in {c.name for c in RuntimeExecution.__table__.columns}
    assert set(RuntimeIdentity.model_fields) == {"name", "email", "status", "confidence"}


# ---------------------------------------------------------------------------
# 2. Authority classification: memory supplies the address, contacts gate it
# ---------------------------------------------------------------------------


async def test_a_name_alone_can_never_produce_an_address() -> None:
    """One matching contact is still UNKNOWN, so ``guests`` cannot yield a recipient.

    ``_exact_claims`` reads only email and phone, and the runtime's fallback
    query carries the name alone. A name match can only ever make the answer
    CONFLICTING, never positive, so the task dies at
    ``attendee identity not established`` before any tool runs.
    """
    async with session_scope() as session:
        backend = GoogleBackend()
        tenant, user = await make_tenant_user("owner@corp.example")
        account = await insert_account(
            tenant_id=tenant.id, user_id=user.id, email="owner@corp.example", sub="sub-1"
        )
        backend.contacts_connections = [
            contact_payload(resource_name="people/1", display_name="Alice", emails=(ALICE,))
        ]
        task = await make_task(tenant_id=tenant.id, user_id=user.id)
        resolved = await IdentityResolver().resolve(
            query=IdentityQuery(name="Alice"),
            context=make_context(
                session=session,
                task=task,
                backend=backend,
                account_id=account.id,
                identity=IdentityQuery(name="Alice"),
            ),
        )
        assert resolved.status is IdentityStatus.UNKNOWN
        assert resolved.email is None


async def test_the_address_is_the_memory_hint_and_the_contact_is_only_a_gate() -> None:
    """The contact's own stored casing never reaches the criterion; the hint does.

    ``_exact_claims`` stores the normalized *query* email as the claim value, so
    ``resolved.email`` is the PERSON-memory hint. The contact proves the address
    exists on the connected account; it does not supply the address, and its other
    addresses are not consulted. This is the finding that separates the three
    authorities on this value: memory is the source, contacts are a gate, and the
    resolver's verdict is only the gate's boolean.
    """
    async with session_scope() as session:
        backend = GoogleBackend()
        tenant, user = await make_tenant_user("owner@corp.example")
        account = await insert_account(
            tenant_id=tenant.id, user_id=user.id, email="owner@corp.example", sub="sub-1"
        )
        # The contact holds the hint *and* a second address it does not mention.
        backend.contacts_connections = [
            contact_payload(
                resource_name="people/1",
                display_name="Alice",
                emails=(ALICE, "alice.work@example.com"),
            )
        ]
        task = await make_task(tenant_id=tenant.id, user_id=user.id)
        query = IdentityQuery(email="  Alice@Example.COM ")
        resolved = await IdentityResolver().resolve(
            query=query,
            context=make_context(
                session=session, task=task, backend=backend, account_id=account.id, identity=query
            ),
        )
        assert resolved.status is IdentityStatus.CONTACT_MATCH
        # The normalized query — i.e. the memory hint — not any address read off
        # the contact record, and not the contact's alternate address.
        assert resolved.email == ALICE
        assert resolved.email != "alice.work@example.com"

        # The same query against a contact list that lacks the address fails the
        # gate, which is the only thing the contact contributes.
        empty = GoogleBackend()
        failed = await IdentityResolver().resolve(
            query=query,
            context=make_context(
                session=session, task=task, backend=empty, account_id=account.id, identity=query
            ),
        )
        assert failed.status is IdentityStatus.UNKNOWN
        assert failed.email is None


def test_the_live_path_can_only_ever_reach_contact_match() -> None:
    """``PROVIDER_VERIFIED`` and ``CHANNEL_VERIFIED`` are unreachable for guests.

    The runtime builds its query with an email or a name and nothing else, so the
    provider and channel branches of the resolver — the two statuses that return
    the *account* address as ``resolved.email`` — can never fire on this path. It
    is pinned as a source-flow fact because a fabricated row can still carry those
    statuses, and nothing in the criteria would notice.
    """
    source = _source(AgentRuntime._resolve_guest_identities)
    assert "IdentityQuery(email=email) if email else IdentityQuery(name=guest)" in source
    for absent in ("provider=", "channel=", "phone=", "provider_account_id="):
        assert absent not in source
    assert '"email": resolved.email or email or ""' in source
    assert "PROVIDER_VERIFIED" not in source
    assert "CHANNEL_VERIFIED" not in source

    tree = ast.parse(source)
    queries = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "IdentityQuery"
    ]
    assert queries, "the runtime must build a resolver query for each guest"
    for call in queries:
        assert {kw.arg for kw in call.keywords} <= {"email", "name"}


# ---------------------------------------------------------------------------
# 3. Recipient intent verdict
# ---------------------------------------------------------------------------


def test_identities_are_a_join_not_an_intent_artifact() -> None:
    """The stored row is four different authorities wearing one name.

    ``name`` is the model's reading, ``email`` is memory content the resolver
    merely confirmed, ``status`` is the resolver's verdict and ``confidence`` its
    score. The criteria read one of the four, which is why a resolver failure
    stops the task while a resolver success never becomes intent.
    """
    criteria = gmail_send_criteria(
        profile="gmail_send",
        identities=[
            {"name": "Alice", "email": ALICE, "status": "CONTACT_MATCH", "confidence": 0.5},
            {
                "name": "Zed Nobody",
                "email": "zed@example.com",
                "status": "CONTACT_MATCH",
                "confidence": 0.5,
            },
        ],
    )
    assert criteria.for_gmail_send().required_recipients == frozenset({ALICE, "zed@example.com"})
    # Dropping the model half entirely changes nothing, because nothing reads it.
    assert gmail_send_criteria(
        profile="gmail_send",
        identities=[
            {"name": "", "email": ALICE, "status": "CONTACT_MATCH", "confidence": 0.5},
            {"name": "", "email": "zed@example.com", "status": "CONTACT_MATCH", "confidence": 0.5},
        ],
    ).for_gmail_send().required_recipients == frozenset({ALICE, "zed@example.com"})


# ---------------------------------------------------------------------------
# 4. Adversarial findings
# ---------------------------------------------------------------------------


def test_the_same_sealed_guest_can_yield_two_different_recipients() -> None:
    """Case B/D: one guest name, two memory hints, two criteria — no system call.

    Nothing distinguishes "the user asked for alice@other" from "memory happened
    to hold alice@other". The sealed intent half of the row is byte-identical in
    both, and only the memory-derived half moves. This is the case the earlier
    round said the code could not express, pinned as what it does say.
    """
    first = gmail_send_criteria(profile="gmail_send", identities=_identities(("Alice", ALICE)))
    second = gmail_send_criteria(
        profile="gmail_send", identities=_identities(("Alice", "alice@other.example"))
    )
    assert _source(gmail_send_criteria).count("identities")
    # Same model output, different address, different criterion.
    assert first.for_gmail_send().required_recipients != second.for_gmail_send().required_recipients
    # And a send that fulfils one is refused by the other.
    record = _send(ALICE)
    assert _verdict(first, record) is FulfillmentVerdict.FULFILLED
    assert _verdict(second, record) is FulfillmentVerdict.NOT_FULFILLED


def test_two_guests_sharing_an_address_and_one_guest_are_the_same_criterion() -> None:
    """Case C: guest multiplicity is destroyed by the frozenset projection.

    ``UnderstandingOutput`` allows five guests. The criterion deduplicates, so
    ``["Alice", "Alicia"]`` resolving to one address is indistinguishable from a
    single guest, and a send to that one address fulfils both.
    """
    two_names_one_address = _required(("Alice", ALICE), ("Alicia", ALICE))
    one_name = _required(("Alice", ALICE))
    assert two_names_one_address == one_name == frozenset({ALICE})
    assert (
        _verdict(
            gmail_send_criteria(
                profile="gmail_send", identities=_identities(("Alice", ALICE), ("Alicia", ALICE))
            ),
            _send(ALICE),
        )
        is FulfillmentVerdict.FULFILLED
    )


def test_two_addresses_are_structurally_unsatisfiable_through_one_recipient_field() -> None:
    """The criterion can demand two addresses that the request cannot carry.

    ``UnderstandingOutput`` admits up to five guests, so two distinct addresses
    are reachable; ``GmailSendArgs.to`` is a single ``EmailStr``, and the
    evaluator's recipient set is therefore always a singleton. Every possible
    spelling of two recipients in that field is either rejected by the schema or
    read as one nonsense address, so the verdict is pinned to NOT_FULFILLED.
    """
    criteria = gmail_send_criteria(
        profile="gmail_send",
        identities=_identities(("Alice", ALICE), ("Bob", "bob@example.com")),
    )
    assert len(criteria.for_gmail_send().required_recipients) == 2
    for recipient in (ALICE, "bob@example.com"):
        assert _verdict(criteria, _send(recipient)) is FulfillmentVerdict.NOT_FULFILLED
    for joined in ("alice@example.com, bob@example.com", "alice@example.com; bob@example.com"):
        with pytest.raises(ValidationError):
            GmailSendArgs(to=joined, subject="s", body="b")
        assert _verdict(criteria, _send(joined)) is FulfillmentVerdict.NOT_FULFILLED, (
            "an unvalidated multi-recipient string must not satisfy two addresses"
        )


def test_status_and_confidence_carry_no_recipient_meaning() -> None:
    """Case E: the resolver's verdict gates the task and is then discarded.

    Across every status and confidence the criteria are identical, because they
    read ``email`` alone. The only way status changes an outcome is the
    ``_POSITIVE_IDENTITY`` gate in the runtime, which raises before the criterion
    is ever built.
    """
    variants: list[dict[str, Any]] = []
    for status in IdentityStatus:
        for confidence in (0.0, 0.5, 1.0):
            variants.append(
                {"name": "Alice", "email": ALICE, "status": status.value, "confidence": confidence}
            )
    required = {
        gmail_send_criteria(profile="gmail_send", identities=[row])
        .for_gmail_send()
        .required_recipients
        for row in variants
    }
    assert required == {frozenset({ALICE})}
    assert (
        _verdict(gmail_send_criteria(profile="gmail_send", identities=[variants[0]]), _send(ALICE))
        is FulfillmentVerdict.FULFILLED
    )

    source = _source(AgentRuntime._resolve_guest_identities)
    assert "_POSITIVE_IDENTITY" in source
    assert "RuntimeFatal" in source


def test_the_criterion_governs_and_the_request_only_reports() -> None:
    """Case: a verified send to the wrong address is refused, and nothing else is.

    ``verified=True`` means the provider echoed *our request*; it says nothing
    about the intent. So the same verified record is FULFILLED or NOT_FULFILLED
    purely by which sealed row produced the criterion, and the durable log — which
    any writer could have produced, on any path — is never taken as the authority.
    """
    criteria = gmail_send_criteria(profile="gmail_send", identities=_identities(("Alice", ALICE)))
    assert _verdict(criteria, _send(ALICE)) is FulfillmentVerdict.FULFILLED
    assert _verdict(criteria, _send("stranger@example.com")) is FulfillmentVerdict.NOT_FULFILLED
    # Unqualified records are refused regardless of the address, and the criterion
    # is not a copy of the request: it names an address the log never carried.
    assert (
        _verdict(criteria, _send(ALICE, record={"verified": False}))
        is FulfillmentVerdict.NOT_FULFILLED
    )
    assert (
        _verdict(
            gmail_send_criteria(profile="gmail_send", identities=_identities(("Alice", ALICE))),
            _send("stranger@example.com"),
        )
        is FulfillmentVerdict.NOT_FULFILLED
    )


def test_prose_and_name_changes_are_invisible_to_the_criterion() -> None:
    """The sealed contract contributes the profile, exactly as it does for calendar.

    ``goal`` and the prose ``success_criteria`` are not inputs to any criteria
    builder, and the guest *name* is not either — so "email Alice about X" and
    "mail Alicia" are the same request to this criterion whenever memory supplies
    the same address.
    """
    from kai.tasks.models import Task

    base = _required(("Alice", ALICE))
    assert base == _required(("Alicia", ALICE))
    assert base == _required(("", ALICE))
    assert (
        gmail_send_criteria(profile="gmail_send", identities=[])
        .for_gmail_send()
        .required_recipients
        == frozenset()
    )
    # The prose the contract does seal is on the task row, and is not read here.
    assert {"goal", "success_criteria", "fulfillment_profile"} <= {
        c.name for c in Task.__table__.columns
    }
    assert "goal" not in _source(gmail_send_criteria)
    assert "success_criteria" not in _source(gmail_send_criteria)


def test_there_is_no_live_request_path_to_construct_a_recipient() -> None:
    """Nothing in the runtime ever produces the ``to`` this criterion is matched against.

    The sealed contract builder pins ``calendar_meeting``, the capability table
    has no gmail action, and the intent branches have no ``gmail.send`` arm — so
    the only live producers of a recipient address are the direct API request and
    the tool adapter, both of which are request data supplied by a caller rather
    than intent read from a sealed row. Pinned as a source-flow fact, because the
    "same intent, different recipient" case is exactly the case a live path would
    have to distinguish, and there is none to ask.
    """
    import kai.runtime.translator as T

    assert T.SLICE_FULFILLMENT_PROFILE == "calendar_meeting"
    assert "gmail_send" not in T.SLICE_CAPABILITIES
    assert not [name for name in T.CAPABILITY_TO_TOOL if "gmail" in name]

    intents = _source(AgentRuntime._intents_for)
    assert "gmail" not in intents

    service = _source(_fulfillment_outcome)
    assert "gmail_send" in service
    # The branch is selected by profile and reads the sealed identities, not the
    # sealed scenario, even though the stages around it still require a scenario.
    branch = service[service.index("criteria = gmail_send_criteria(") :][:220]
    assert "scenario" not in branch
    assert "identities=identities" in branch

    # The two real producers of a `to` carry no intent: they take it from the caller.
    adapter = _source(__import__("kai.tools.adapters.gmail", fromlist=["x"]))
    assert 'arguments["to"]' in adapter
    api = _source(__import__("kai.api.gmail", fromlist=["x"]))
    assert "send_and_verify" in api
    assert "RuntimeExecution" not in api
    assert set(GmailSendArgs.model_fields) == {"to", "subject", "body"}


# ---------------------------------------------------------------------------
# 5. The license boundary is the intent-input property (§10.10)
# ---------------------------------------------------------------------------


def test_live_sealing_requires_an_intent_projected_criterion_value() -> None:
    """The license rule, stated as an executable fact about the code.

    ``LIVE_SEALABLE_PROFILES`` is the invariant's operational shadow: a profile
    is listed only when at least one of its criterion values is a projection of
    the sealed user-intent understanding — a builder input the understanding
    stage produced, which its evaluator then decides on. ``calendar_meeting``
    has ``window`` (:= ``scenario.window``) and the calendar decision reads it;
    ``gmail_send`` has no intent-class input at all, so it stays out and a
    contract naming it is refused at ``create_task``.
    """
    assert frozenset({"calendar_meeting"}) == LIVE_SEALABLE_PROFILES

    # calendar: a builder input that is a projection of the sealed understanding.
    assert "window" in inspect.signature(calendar_meeting_criteria).parameters
    # gmail: no such input exists, so no criterion value can be intent-class.
    assert set(inspect.signature(gmail_send_criteria).parameters) == {
        "profile",
        "identities",
    }

    # The second half of the rule: the evaluator decides on the projected value.
    assert "criteria.window" in _source(_meeting_satisfies_criteria)
    assert "window" not in _source(_send_satisfies_criteria)


def test_a_replayed_identity_row_re_seals_the_same_derived_criterion() -> None:
    """Replay re-reads the same bytes and cannot promote them into intent.

    A resumed run rebuilds ``required_recipients`` from the durable identity
    rows exactly as the live run did. Round-tripping those rows through the
    same serialization a database read performs must reproduce the identical
    criterion and verdict — and the upgrade is structurally impossible, because
    the builder's only inputs are ``profile`` and ``identities``: the sealed
    understanding has no path into this criterion, so nothing a replay finds
    there could re-classify the derived address as sealed intent.
    """
    live = _required(("Alice", ALICE))
    recovered_rows = json.loads(json.dumps(_identities(("Alice", ALICE))))
    recovered = _required_from(recovered_rows)
    assert recovered == live == frozenset({ALICE})
    assert (
        _verdict(gmail_send_criteria(profile="gmail_send", identities=recovered_rows), _send(ALICE))
        is FulfillmentVerdict.FULFILLED
    )
    assert set(inspect.signature(gmail_send_criteria).parameters) == {
        "profile",
        "identities",
    }
