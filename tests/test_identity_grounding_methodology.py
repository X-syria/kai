"""The identity grounding methodology: one entity, or a set of matching claims?

Every other part of the resolver agrees that the answer to "who is this" is
**one entity**. The conflict rule counts distinct contacts. ``_build`` derives
``contact_id`` from distinct contacts. ``IdentityStatus`` documents itself as
"the deterministic verdict the resolver can vouch for about *one entity*", and
``resolve`` says it "resolve[s] *one entity* against trustable sources".
``IdentityResult`` is structurally single-valued — one ``contact_id``, one
``email``, one ``phone`` — so it cannot represent two identities even in
principle. And ``derive_confidence`` treats agreement between sources as a
**bonus** (``min(1.0, base + 0.1)``); nothing in it reduces trust for finding
more corroboration.

Against that stands one line. The status predicate counted *claims*, so a
contact matched on both email and phone — two assertions about one person, and
the case ``derive_confidence`` exists to reward — fell to ``UNKNOWN`` at zero
confidence **while ``contact_id`` was still populated on the same result**. The
value object asserted "I found this person" in ``contact_id``, ``matched_by``
and ``evidence`` and "I found nobody" in ``status``, simultaneously.

So the question this file settles is not a preference. It is whether one line
disagreed with the function it sits inside.

The answer is measured here, and the blast radius is measured too: a two-claim
query requires a query carrying *both* an email and a phone, and every
``IdentityQuery`` the product builds is email-only or name-only. The defect was
therefore latent, which is why fixing it changes no live behaviour — and also
why leaving it in place is a trap for whoever first adds a phone to the guest
query.

Nothing here changes the vocabulary, the conflict rule, the confidence
arithmetic, the policy projection, or any reader.
"""

from __future__ import annotations

import inspect
import textwrap
from typing import Any, Final
from uuid import uuid4

import pytest
from pydantic import BaseModel

from kai.contacts.models import Contact
from kai.db.models import GoogleAccount
from kai.identity import IdentityQuery, IdentityResolver, IdentityStatus
from kai.identity.service import _exact_claims, _has_conflict
from kai.policy.models import IdentityStatus as PolicyIdentityStatus
from kai.policy.rules import rule_identity
from kai.runtime.service import AgentRuntime
from kai.tools import RiskLevel, ToolDefinition, VerificationContract
from kai.tools.gateway import _IDENTITY_TO_POLICY
from tool_harness import (
    GoogleBackend,
    contact_payload,
    insert_account,
    make_context,
    make_intent,
    make_policy_input,
    make_task,
    make_tenant_user,
    session_scope,
)

ALICE: Final = "alice@example.com"
BOB: Final = "bob@example.com"
PHONE: Final = "+15550100"


class _SendArgs(BaseModel):
    """Minimal schema: only the policy engine is exercised, never execution."""

    to: str
    subject: str
    body: str


class _NeverRun:
    """An executor that must never be reached by a policy-only test."""

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("rule_identity must not execute the tool")

#: The exact entity metric the conflict rule uses (C1) and the status predicate
#: now shares with it.
ENTITY_METRIC: Final = "{claim.contact.resource_name for claim in exact_claims}"


def _source(node: Any) -> str:
    return textwrap.dedent(inspect.getsource(node))


def _contact(
    *,
    resource_name: str,
    display_name: str = "Alice",
    emails: tuple[str, ...] = (),
    phones: tuple[str, ...] = (),
) -> dict[str, Any]:
    return contact_payload(
        resource_name=resource_name, display_name=display_name, emails=emails, phones=phones
    )


def _backend(connections: list[dict[str, Any]]) -> GoogleBackend:
    backend = GoogleBackend()
    backend.contacts_connections = connections
    return backend


async def test_one_exact_claim_on_one_entity_is_a_positive_identity() -> None:
    """Case 1 — the baseline the correction must not disturb.

    A single exact email against a single contact is ``CONTACT_MATCH`` at the
    contact evidence strength, and it carries both the entity id and the address.
    """
    async with session_scope() as session:
        tenant, user = await make_tenant_user("owner-1@corp.example")
        account = await insert_account(
            tenant_id=tenant.id, user_id=user.id, email="owner-1@corp.example", sub="sub-1"
        )
        task = await make_task(tenant_id=tenant.id, user_id=user.id)
        query = IdentityQuery(email=ALICE)
        result = await IdentityResolver().resolve(
            query=query,
            context=make_context(
                session=session, task=task, backend=_backend([_contact(
                    resource_name="people/1", emails=(ALICE,)
                )]),
                account_id=account.id, identity=query,
            ),
        )
        assert result.status is IdentityStatus.CONTACT_MATCH
        assert result.confidence == 0.5
        assert result.contact_id == "people/1"
        assert result.email == ALICE


async def test_corroborating_claims_about_one_entity_are_one_identity() -> None:
    """Case 2 — the methodology, stated as behaviour.

    One contact carrying both the queried email and the queried phone produces two
    exact claims and exactly one entity. That is one identity established twice,
    so it is ``CONTACT_MATCH`` — the state whose own definition is "a trusted
    identifier (exact email or exact phone) appears in the tenant's Google
    Contacts", satisfied here twice over.

    The result is also now internally consistent: ``status``, ``contact_id`` and
    ``matched_by`` all describe the same single entity. Before the correction this
    case returned ``UNKNOWN`` with ``contact_id`` populated, which made one value
    object deny and assert the match at the same time.
    """
    async with session_scope() as session:
        tenant, user = await make_tenant_user("owner-2@corp.example")
        account = await insert_account(
            tenant_id=tenant.id, user_id=user.id, email="owner-2@corp.example", sub="sub-2"
        )
        task = await make_task(tenant_id=tenant.id, user_id=user.id)
        contacts = [_contact(resource_name="people/1", emails=(ALICE,), phones=(PHONE,))]
        query = IdentityQuery(email=ALICE, phone=PHONE)

        result = await IdentityResolver().resolve(
            query=query,
            context=make_context(
                session=session, task=task, backend=_backend(contacts),
                account_id=account.id, identity=query,
            ),
        )

        # Two claims really were made, against one entity really. Counted from the
        # evidence the resolver itself emitted, so the assertion is about behaviour
        # and not about re-deriving the claim construction.
        contact_claims = [e for e in result.evidence if e.contact_id is not None]
        assert len(contact_claims) == 2
        assert {e.contact_id for e in contact_claims} == {"people/1"}

        # One entity, and therefore one identity.
        assert result.status is IdentityStatus.CONTACT_MATCH
        assert result.contact_id == "people/1"
        assert result.email == ALICE
        # The phone is reported in normalised E.164 digits, without the "+".
        assert result.phone == PHONE.lstrip("+")
        assert result.matched_by == "contact_email_exact"

        # Confidence is the fixed contact strength. Two identifiers against one
        # source are not "two independent trusted sources", so the corroboration
        # bonus in derive_confidence correctly does not apply — it keys on source,
        # not on claim count, and it only ever adds.
        assert result.confidence == 0.5

        # And the status now agrees with the entity the result names.
        assert (result.status is not IdentityStatus.UNKNOWN) == (result.contact_id is not None)


async def test_two_distinct_entities_remain_a_conflict_and_never_pick_a_winner() -> None:
    """Case 3 — the asymmetry the methodology depends on.

    Two contacts, each matched on a different identifier, is a genuinely different
    condition: the query's two identifiers point at **two different people**, so
    the resolver must not choose. That is ``CONFLICTING``, and it is decided
    before the status predicate runs, so the correction cannot soften it.
    """
    async with session_scope() as session:
        tenant, user = await make_tenant_user("owner-3@corp.example")
        account = await insert_account(
            tenant_id=tenant.id, user_id=user.id, email="owner-3@corp.example", sub="sub-3"
        )
        task = await make_task(tenant_id=tenant.id, user_id=user.id)
        contacts = [
            _contact(resource_name="people/1", display_name="Alice", emails=(ALICE,)),
            _contact(resource_name="people/2", display_name="Bob", emails=(), phones=(PHONE,)),
        ]
        query = IdentityQuery(email=ALICE, phone=PHONE)

        result = await IdentityResolver().resolve(
            query=query,
            context=make_context(
                session=session, task=task, backend=_backend(contacts),
                account_id=account.id, identity=query,
            ),
        )

        assert result.status is IdentityStatus.CONFLICTING
        assert result.confidence == 0.0
        # Both identifiers were found, and both claims were recorded — this is not
        # a near miss. C1 rejected them anyway because they name two entities, so
        # no entity is named in the result and no winner is chosen.
        assert len([e for e in result.evidence if e.contact_id is not None]) == 2
        assert {e.contact_id for e in result.evidence if e.contact_id} == {"people/1", "people/2"}
        assert result.contact_id is None


async def test_a_shared_identifier_across_two_contacts_is_still_a_conflict() -> None:
    """Case 3b — one identifier, two claimants, is not corroboration.

    This is the case that keeps the methodology honest. Two contacts holding the
    *same* address produce two claims that agree with each other, so a naive
    "do the claims agree?" reading would call that positive. It is not: the claims
    do not agree about *who*, they disagree about which record is the person.
    Distinct entities, so ``CONFLICTING`` — decided by C1 on the same metric the
    status predicate now uses.
    """
    async with session_scope() as session:
        tenant, user = await make_tenant_user("owner-4@corp.example")
        account = await insert_account(
            tenant_id=tenant.id, user_id=user.id, email="owner-4@corp.example", sub="sub-4"
        )
        task = await make_task(tenant_id=tenant.id, user_id=user.id)
        contacts = [
            _contact(resource_name="people/1", display_name="Alice", emails=(ALICE,)),
            _contact(resource_name="people/2", display_name="Alice w", emails=(ALICE,)),
        ]
        query = IdentityQuery(email=ALICE)

        result = await IdentityResolver().resolve(
            query=query,
            context=make_context(
                session=session, task=task, backend=_backend(contacts),
                account_id=account.id, identity=query,
            ),
        )
        assert result.status is IdentityStatus.CONFLICTING
        assert result.contact_id is None


async def test_no_exact_claim_is_unknown_and_a_name_never_grounds() -> None:
    """Case 4 — absence stays absence.

    Zero exact claims means zero entities, so there is nothing to be positive
    about. A name is carried for the record and can only widen ambiguity; it can
    never produce a claim, because ``_exact_claims`` reads email and phone only.
    """
    async with session_scope() as session:
        tenant, user = await make_tenant_user("owner-5@corp.example")
        account = await insert_account(
            tenant_id=tenant.id, user_id=user.id, email="owner-5@corp.example", sub="sub-5"
        )
        task = await make_task(tenant_id=tenant.id, user_id=user.id)
        contacts = [_contact(resource_name="people/1", display_name="Alice", emails=(ALICE,))]

        async def _resolve(query: IdentityQuery) -> Any:
            return await IdentityResolver().resolve(
                query=query,
                context=make_context(
                    session=session, task=task, backend=_backend(contacts),
                    account_id=account.id, identity=query,
                ),
            )

        # A name that matches: still nothing to ground on.
        by_name = await _resolve(IdentityQuery(name="Alice"))
        assert by_name.status is IdentityStatus.UNKNOWN
        assert by_name.email is None
        assert by_name.contact_id is None

        # An address no contact holds: nothing matched, nothing populated.
        by_absent = await _resolve(IdentityQuery(email="zed@nowhere.example"))
        assert by_absent.status is IdentityStatus.UNKNOWN
        assert by_absent.email is None

        # No identifier at all.
        empty = await _resolve(IdentityQuery())
        assert empty.status is IdentityStatus.UNKNOWN
        assert empty.confidence == 0.0


async def test_a_populated_address_cannot_override_the_identity_decision() -> None:
    """Case 5 — the address is a payload, never a source of authority.

    Two entities matched on two identifiers resolve to ``CONFLICTING``. The
    resolver no longer carries the queried email out on that verdict at all:
    a status that selected nothing publishes no identifier, which is the same
    rule the ambiguous-name case below already relied on. Confidence stays zero
    and no entity is named. The runtime's single writer refuses the row on
    status, and there is now no address for a downstream reader to seize on.
    """
    async with session_scope() as session:
        tenant, user = await make_tenant_user("owner-6@corp.example")
        account = await insert_account(
            tenant_id=tenant.id, user_id=user.id, email="owner-6@corp.example", sub="sub-6"
        )
        task = await make_task(tenant_id=tenant.id, user_id=user.id)
        contacts = [
            _contact(resource_name="people/1", display_name="Alice", emails=(ALICE,)),
            _contact(resource_name="people/2", display_name="Bob", emails=(), phones=(PHONE,)),
        ]
        query = IdentityQuery(email=ALICE, phone=PHONE)

        result = await IdentityResolver().resolve(
            query=query,
            context=make_context(
                session=session, task=task, backend=_backend(contacts),
                account_id=account.id, identity=query,
            ),
        )
        # No winner, so nothing that would name one leaves the resolver.
        assert result.status is IdentityStatus.CONFLICTING
        assert result.email is None
        assert result.phone is None
        assert result.contact_id is None
        assert result.confidence == 0.0

        # The same must hold when the only thing supplied is a display name that
        # widens ambiguity: a name can never ground an identity, so a fuzzy match
        # over two people stays a conflict and never populates an address.
        ambiguous_name = await IdentityResolver().resolve(
            query=IdentityQuery(name="Alice"),
            context=make_context(
                session=session, task=task,
                backend=_backend([
                    _contact(resource_name="people/1", display_name="Alice", emails=(ALICE,)),
                    _contact(resource_name="people/2", display_name="Alice w", emails=(BOB,)),
                ]),
                account_id=account.id, identity=IdentityQuery(name="Alice"),
            ),
        )
        assert ambiguous_name.status is IdentityStatus.CONFLICTING
        assert ambiguous_name.confidence == 0.0
        assert ambiguous_name.email is None
        assert ambiguous_name.contact_id is None


async def test_replay_preserves_the_identity_authority_class_of_a_sealed_row() -> None:
    """Case 6 — the correction is a resolution-time decision, not a stored one.

    Whatever class the resolver returns is what gets written, and replay reuses
    the written row verbatim: ``_drive`` re-resolves only while the row is empty.
    So a corroborated guest that now resolves positively is persisted as positive
    and read back as positive, and a conflicting one is never persisted at all.
    The authority class cannot drift between the first resolution and every
    subsequent read.
    """
    drive = _source(AgentRuntime._drive)
    assert drive.count("if not row.identities:") == 1
    assert "if row.identities:" not in drive
    assert "row.identities = identities" in drive
    # On the replay path the row is read, never re-resolved.
    assert _source(AgentRuntime._execute).count("list(row.identities)") == 1
    assert _source(AgentRuntime._settle_verification).count("list(row.identities)") == 1
    assert "resolver.resolve(" not in _source(AgentRuntime._settle_verification)

    # The writer gates on status, and both statuses this round can produce are
    # handled by that one gate.
    resolve_guests = _source(AgentRuntime._resolve_guest_identities)
    assert "if resolved.status not in _POSITIVE_IDENTITY:" in resolve_guests
    assert resolve_guests.count('"status": resolved.status.value') == 1


async def test_the_policy_projection_is_untouched_and_still_total() -> None:
    """Case 7 — the correction must not reach the policy layer.

    The resolver's ``IdentityStatus`` and the policy layer's are **two separate
    enums**, and ``_IDENTITY_TO_POLICY`` is the only bridge between them. That
    indirection is the reason the correction is contained: the resolver changed
    which of its own five states it emits for one input, and the projection is a
    total map that was not re-pointed, renamed, or extended. No state was added on
    either side.
    """
    # The resolver's vocabulary, unchanged.
    assert {s.value for s in IdentityStatus} == {
        "CONTACT_MATCH", "PROVIDER_VERIFIED", "CHANNEL_VERIFIED", "UNKNOWN", "CONFLICTING",
    }
    for absent in ("RESOLVED", "AMBIGUOUS", "UNAUTHORIZED", "PARTIAL", "MATCHED"):
        assert not hasattr(IdentityStatus, absent)

    # The policy layer's vocabulary, unchanged, and still total over the resolver's.
    assert {s.value for s in PolicyIdentityStatus} == {"VERIFIED", "UNKNOWN", "CONFLICTING"}
    assert set(_IDENTITY_TO_POLICY) == set(IdentityStatus)
    assert all(isinstance(k, IdentityStatus) for k in _IDENTITY_TO_POLICY)
    assert all(isinstance(v, PolicyIdentityStatus) for v in _IDENTITY_TO_POLICY.values())
    assert {k.value: v.value for k, v in _IDENTITY_TO_POLICY.items()} == {
        "CONTACT_MATCH": "VERIFIED",
        "CHANNEL_VERIFIED": "VERIFIED",
        "PROVIDER_VERIFIED": "VERIFIED",
        "UNKNOWN": "UNKNOWN",
        "CONFLICTING": "CONFLICTING",
    }

    # And the whole resolve -> project -> rule path, at MEDIUM risk.
    async with session_scope():
        tenant, user = await make_tenant_user("owner-7@corp.example")
        task = await make_task(tenant_id=tenant.id, user_id=user.id, risk_policy="MEDIUM")
        tool = ToolDefinition(
            name="gmail.send",
            description="Send an email to a resolved recipient.",
            input_schema=_SendArgs,
            risk_level=RiskLevel.MEDIUM,
            capability="gmail",
            executor=_NeverRun(),
            verification_contract=VerificationContract.READ_AFTER_SEND,
        )
        intent = make_intent("gmail.send", task=task)

        def _outcome(status: IdentityStatus, risk: RiskLevel) -> str:
            """Exactly the sequence gateway._resolve_recipient_identity performs."""
            pol = make_policy_input(
                task=task, tool=tool, intent=intent, risk_level=risk,
                recipient_identity=_IDENTITY_TO_POLICY[status],
            )
            decision = rule_identity(pol)
            return "ALLOW" if decision is None else decision.decision.value

        # Every positive class passes. UNKNOWN escalates. CONFLICTING denies.
        assert _outcome(IdentityStatus.CONTACT_MATCH, RiskLevel.MEDIUM) == "ALLOW"
        assert _outcome(IdentityStatus.PROVIDER_VERIFIED, RiskLevel.MEDIUM) == "ALLOW"
        assert _outcome(IdentityStatus.CHANNEL_VERIFIED, RiskLevel.MEDIUM) == "ALLOW"
        assert _outcome(IdentityStatus.UNKNOWN, RiskLevel.MEDIUM) == "REQUIRE_APPROVAL"
        assert _outcome(IdentityStatus.CONFLICTING, RiskLevel.MEDIUM) == "DENY"

        # Below MEDIUM nothing is enforced, so the classes that would otherwise
        # escalate or deny pass. Identity is not the thing being careful at LOW.
        assert _outcome(IdentityStatus.UNKNOWN, RiskLevel.LOW) == "ALLOW"
        assert _outcome(IdentityStatus.CONFLICTING, RiskLevel.LOW) == "ALLOW"

    # The point of the correction, expressed at the boundary: a contact that matched
    # on one identifier and a contact that matched on two now reach the same place,
    # because they are the same semantic event — one entity, positively grounded.
    assert _IDENTITY_TO_POLICY[IdentityStatus.CONTACT_MATCH] is PolicyIdentityStatus.VERIFIED


def test_no_product_query_can_yield_two_claims_so_the_correction_is_latent() -> None:
    """Case 8 — why this changed no live behaviour, and why it still matters.

    A two-claim result needs a query carrying both an email and a phone. Every
    ``IdentityQuery`` the product builds is email-only or name-only, so the
    corrected branch was unreachable in production.

    That is the reason this is safe, and also the trap: the first person to add a
    phone to the guest query would have got a silently false ``UNKNOWN`` for the
    best-corroborated contact in the address book, and it would have read as a
    resolver bug rather than as a wrong metric. Pinned here so the premise is
    explicit rather than incidental.
    """
    resolve_guests = _source(AgentRuntime._resolve_guest_identities)
    assert "IdentityQuery(email=email) if email else IdentityQuery(name=guest)" in resolve_guests
    for never_set in ("phone=", "provider=", "channel=", "provider_account_id="):
        assert never_set not in resolve_guests

    intents_for = _source(AgentRuntime._intents_for)
    assert "queried_identity = IdentityQuery(email=emails[0])" in intents_for

    # The only fields that can produce a claim.
    exact = _source(_exact_claims)
    assert "_norm_email(query.email)" in exact
    assert "_norm_phone(query.phone)" in exact
    assert "query.name" not in exact, "a name must never produce a claim"
    assert "query.provider_account_id" not in exact
    assert "query.channel" not in exact


def test_the_status_predicate_and_the_conflict_rule_are_the_same_rule() -> None:
    """The invariant this round establishes, stated once.

    Before the correction the resolver asked one question two ways and disagreed
    with itself: C1 counted distinct contacts to detect a conflict, while the
    status predicate counted claims, and ``_build`` counted distinct contacts again
    to name the entity. One input could therefore produce a result that said
    "UNKNOWN" and "here is the contact I matched" at the same time.

    Now the status predicate reads the same set C1 reads, so the resolver has
    exactly one notion of "how many entities did we match", and ``status`` and
    ``contact_id`` cannot diverge.
    """
    resolve = _source(IdentityResolver.resolve)
    build = _source(IdentityResolver._build)
    conflict = _source(_has_conflict)

    # The status predicate.
    assert f"elif len({ENTITY_METRIC}) == 1:" in resolve
    assert "len(exact_claims) == 1" not in resolve

    # The conflict rule, unchanged.
    assert f"if len({ENTITY_METRIC}) > 1:" in conflict

    # The entity the result names, unchanged.
    assert f"distinct_ids = {ENTITY_METRIC}" in build

    # One question, one metric, three uses.
    assert resolve.count(ENTITY_METRIC) == 1
    assert conflict.count(ENTITY_METRIC) == 1
    assert build.count(ENTITY_METRIC) == 1


# ---------------------------------------------------------------------------
# The producer contract: a verdict that selected nothing names nothing
# ---------------------------------------------------------------------------


_SELECTING: Final = (
    IdentityStatus.PROVIDER_VERIFIED,
    IdentityStatus.CHANNEL_VERIFIED,
    IdentityStatus.CONTACT_MATCH,
)
_NOT_SELECTING: Final = (IdentityStatus.CONFLICTING, IdentityStatus.UNKNOWN)


def _claims_for(*contacts: Contact) -> list[Any]:
    """Exact claims over real ``Contact`` rows, as the resolver sees them."""
    return _exact_claims(IdentityQuery(email=ALICE), list(contacts))


def _entity(resource_name: str, display_name: str, *emails: str) -> Contact:
    return Contact(
        resource_name=resource_name,
        display_name=display_name,
        emails=emails,
        phone_numbers=(),
    )


@pytest.mark.parametrize("status", _NOT_SELECTING)
def test_a_verdict_that_selected_nothing_publishes_no_identifier(
    status: IdentityStatus,
) -> None:
    """The contract, stated once: no winner means no address, phone, or contact.

    ``_has_conflict`` promises CONFLICTING never picks a winner. UNKNOWN never
    picked one either; it is the verdict for "nothing constrained the answer".
    Both used to publish identifiers anyway, because ``_build`` populated them
    from the claims without asking the status. Enforcing it at the producer is
    what lets every downstream reader keep treating the shape as trustworthy
    instead of defensively re-deriving whether a value may be consumed.
    """
    resolver = IdentityResolver.__new__(IdentityResolver)
    claims = _claims_for(
        _entity("people/1", "Alice", ALICE),
        _entity("people/2", "Alice too", ALICE),
    )

    result = resolver._build(status, tuple(c.evidence for c in claims), claims, None)

    assert result.status is status
    assert result.email is None
    assert result.phone is None
    assert result.contact_id is None
    assert result.display_name is None
    # Confidence was already zero for both verdicts and stays derived from
    # evidence, never hand-set to make this test pass.
    assert result.confidence == 0.0
    # Refusing to select is not the same as forgetting: the evidence survives so
    # the conflict remains auditable.
    assert result.evidence


@pytest.mark.parametrize("status", _SELECTING)
def test_a_verdict_that_selected_an_entity_still_names_it(status: IdentityStatus) -> None:
    """The other half of the contract, so the fix cannot be over-applied.

    A tightening that silenced every status would satisfy the CONFLICTING rule
    and quietly break every positive resolution, leaving nothing able to fulfil
    anything. These three must keep publishing, and they must keep publishing an
    address: the account-bound pair from the account, the contact-bound one from
    the claim that established it.
    """
    resolver = IdentityResolver.__new__(IdentityResolver)
    claims = _claims_for(_entity("people/1", "Alice", ALICE))
    account = GoogleAccount(
        id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        google_sub="sub-owner",
        email="owner@corp.example",
        scopes="https://www.googleapis.com/auth/calendar",
        credentials_encrypted="{}",
    )

    result = resolver._build(
        status, tuple(c.evidence for c in claims), claims, account
    )

    assert result.status is status
    assert result.contact_id == "people/1"
    assert result.display_name == "Alice"
    assert result.confidence > 0.0
    assert result.email is not None
    if status is IdentityStatus.CONTACT_MATCH:
        assert result.email == ALICE
    else:
        # Account-bound verdicts name the connected account, not the claim.
        assert result.email == "owner@corp.example"
