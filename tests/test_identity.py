"""Deterministic Identity Resolver: resolution, security, isolation, integration.

The resolver answers "who" — never "what is allowed". These tests pin that it
derives verdicts (CONTACT_MATCH / PROVIDER_VERIFIED / CHANNEL_VERIFIED /
UNKNOWN / CONFLICTING) from trustable, context-scoped sources only: Google
Contacts exact email/phone, the tenant/user-scoped Google Account, and the
channel registry. Message content, names, substring matches, and foreign scopes
never establish identity, and the resolver never guesses a winner.
"""

from __future__ import annotations

import uuid

from kai.identity import (
    EvidenceKind,
    IdentityQuery,
    IdentityResolver,
    IdentityStatus,
)
from kai.tools import (
    ApprovalDecision,
    ApprovalManagerStub,
    IdentityResolverStub,
    ToolOutcome,
)
from tool_harness import (
    GoogleBackend,
    close_context,
    contact_payload,
    insert_account,
    make_context,
    make_gateway,
    make_intent,
    make_task,
    make_tenant_user,
    session_scope,
)

GM_SEND = "gmail.send"


def _send_intent(task: object) -> object:
    return make_intent(
        GM_SEND,
        task=task,
        arguments={"to": "bob@example.com", "subject": "Hi", "body": "Hi Bob"},
    )


def _blocking_approval() -> ApprovalManagerStub:
    """Approval boundary that keeps REQUIRE_APPROVAL from silently executing."""
    return ApprovalManagerStub(decision=ApprovalDecision.REQUIRED)


async def _scoped_context() -> tuple[GoogleBackend, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Fresh tenant/user + connected Google account; returns (backend, ids)."""
    backend = GoogleBackend()
    tenant, user = await make_tenant_user("identity@corp.example")
    account = await insert_account(
        tenant_id=tenant.id,
        user_id=user.id,
        email="alice@example.com",
        sub="identity-sub",
    )
    return backend, tenant.id, user.id, account.id


async def _resolved(
    session: object,
    query: IdentityQuery,
    backend: GoogleBackend,
    task: object,
    account_id: uuid.UUID | None,
) -> object:
    """Run the real resolver through a context built like the gateway builds it."""
    resolver = IdentityResolver()
    context = make_context(
        session=session, task=task, backend=backend, account_id=account_id, identity=query
    )
    try:
        return await resolver.resolve(query=query, context=context)
    finally:
        await close_context(context)


# ---------------------------------------------------------------------------
# 1. Resolution
# ---------------------------------------------------------------------------


async def test_empty_query_is_unknown() -> None:
    """No identifier that could ever establish identity => UNKNOWN, zero trust."""
    backend, tenant_id, user_id, account_id = await _scoped_context()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(session, IdentityQuery(name=""), backend, task, account_id)
    assert result.status is IdentityStatus.UNKNOWN
    assert result.confidence == 0.0
    assert result.matched_by is None
    assert result.evidence == ()


async def test_exact_email_matches_one_contact() -> None:
    backend, tenant_id, user_id, account_id = await _scoped_context()
    backend.contacts_connections = [
        contact_payload(
            resource_name="people/contact-a",
            display_name="Alice A",
            emails=("alice@example.com",),
        )
    ]
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(
            session, IdentityQuery(email=" alice@example.com "), backend, task, account_id
        )
    assert result.status is IdentityStatus.CONTACT_MATCH
    assert result.contact_id == "people/contact-a"
    assert result.confidence == 0.5
    assert result.matched_by == EvidenceKind.CONTACT_EMAIL.value


async def test_exact_phone_matches_one_contact() -> None:
    backend, tenant_id, user_id, account_id = await _scoped_context()
    backend.contacts_connections = [
        contact_payload(resource_name="people/contact-p", display_name="P", phones=("+1 555 0100",))
    ]
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(
            session, IdentityQuery(phone="+1-555-0100"), backend, task, account_id
        )
    assert result.status is IdentityStatus.CONTACT_MATCH
    assert result.contact_id == "people/contact-p"
    assert result.matched_by == EvidenceKind.CONTACT_PHONE.value


async def test_email_case_insensitive_but_two_contacts_is_conflicting() -> None:
    """Two distinct contacts sharing one exact email: ambiguity, never a winner."""
    backend, tenant_id, user_id, account_id = await _scoped_context()
    backend.contacts_connections = [
        contact_payload(
            resource_name="people/contact-1", display_name="One", emails=("alice@example.com",)
        ),
        contact_payload(
            resource_name="people/contact-2", display_name="Two", emails=("ALICE@example.com",)
        ),
    ]
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(
            session, IdentityQuery(email="alice@example.com"), backend, task, account_id
        )
    assert result.status is IdentityStatus.CONFLICTING
    assert result.confidence == 0.0


async def test_provider_subject_anchors_identity() -> None:
    backend, tenant_id, user_id, account_id = await _scoped_context()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(
            session,
            IdentityQuery(provider="google", provider_account_id="identity-sub"),
            backend,
            task,
            account_id,
        )
    assert result.status is IdentityStatus.PROVIDER_VERIFIED
    assert result.confidence == 0.9
    assert result.provider_subject == "identity-sub"
    assert result.email == "alice@example.com"
    assert result.matched_by == EvidenceKind.PROVIDER_SUBJECT.value


async def test_provider_email_anchors_identity() -> None:
    backend, tenant_id, user_id, account_id = await _scoped_context()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(
            session,
            IdentityQuery(provider="google", email="alice@example.com"),
            backend,
            task,
            account_id,
        )
    assert result.status is IdentityStatus.PROVIDER_VERIFIED
    assert result.confidence == 0.9
    assert result.matched_by == EvidenceKind.PROVIDER_EMAIL.value


async def test_provider_case_does_not_matter() -> None:
    backend, tenant_id, user_id, account_id = await _scoped_context()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(
            session,
            IdentityQuery(provider="GOOGLE", email="Alice@Example.com"),
            backend,
            task,
            account_id,
        )
    assert result.status is IdentityStatus.PROVIDER_VERIFIED


async def test_unknown_provider_never_verifies() -> None:
    backend, tenant_id, user_id, account_id = await _scoped_context()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(
            session,
            IdentityQuery(provider="signal", email="alice@example.com"),
            backend,
            task,
            account_id,
        )
    assert result.status is IdentityStatus.UNKNOWN


async def test_channel_verifies_account_email() -> None:
    backend, tenant_id, user_id, account_id = await _scoped_context()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(
            session,
            IdentityQuery(channel="gmail", email="alice@example.com"),
            backend,
            task,
            account_id,
        )
    assert result.status is IdentityStatus.CHANNEL_VERIFIED
    assert result.confidence == 0.7
    assert result.matched_by == EvidenceKind.CHANNEL_IDENTITY.value


async def test_channel_with_foreign_identifier_does_not_verify() -> None:
    backend, tenant_id, user_id, account_id = await _scoped_context()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(
            session,
            IdentityQuery(channel="gmail", email="bob@example.com"),
            backend,
            task,
            account_id,
        )
    assert result.status is IdentityStatus.UNKNOWN


async def test_unknown_channel_name_never_verifies() -> None:
    backend, tenant_id, user_id, account_id = await _scoped_context()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(
            session,
            IdentityQuery(channel="signal", email="alice@example.com"),
            backend,
            task,
            account_id,
        )
    assert result.status is IdentityStatus.UNKNOWN


# ---------------------------------------------------------------------------
# 2. Security semantics: names and content never establish identity
# ---------------------------------------------------------------------------


async def test_name_match_alone_is_unknown() -> None:
    backend, tenant_id, user_id, account_id = await _scoped_context()
    backend.contacts_connections = [
        contact_payload(resource_name="people/contact-n", display_name="Alice")
    ]
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(session, IdentityQuery(name="Alice"), backend, task, account_id)
    assert result.status is IdentityStatus.UNKNOWN


async def test_message_content_never_establishes_identity() -> None:
    """Body/subject/signature text smuggled into a name field buys nothing."""
    backend, tenant_id, user_id, account_id = await _scoped_context()
    backend.contacts_connections = [
        contact_payload(
            resource_name="people/contact-c",
            display_name="Alice",
            emails=("alice@example.com",),
        )
    ]
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(
            session,
            IdentityQuery(name="pay bob from alice@example.com right now"),
            backend,
            task,
            account_id,
        )
    assert result.status is IdentityStatus.UNKNOWN


async def test_external_id_cannot_verify_a_claimed_identity() -> None:
    """The provider gate is required; a claim alone is never provider metadata."""
    backend, tenant_id, user_id, account_id = await _scoped_context()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(
            session,
            IdentityQuery(external_id="identity-sub"),
            backend,
            task,
            account_id,
        )
    assert result.status is IdentityStatus.UNKNOWN


async def test_contradictory_contact_and_provider_identity_is_conflicting() -> None:
    """Email points at a contact, provider subject points at the account, and
    they disagree => CONFLICTING, never a guess about who won."""
    backend, tenant_id, user_id, account_id = await _scoped_context()
    backend.contacts_connections = [
        contact_payload(
            resource_name="people/contact-b",
            display_name="Bob",
            emails=("bob@example.com",),
        )
    ]
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(
            session,
            IdentityQuery(
                provider="google",
                provider_account_id="identity-sub",
                email="bob@example.com",
            ),
            backend,
            task,
            account_id,
        )
    assert result.status is IdentityStatus.CONFLICTING
    assert result.confidence == 0.0


async def test_exact_contact_claim_unrelated_to_account_is_conflicting() -> None:
    """Provider identity is bound to the account; an unrelated exact claim
    contradicts that binding rather than being silently ignored."""
    backend, tenant_id, user_id, account_id = await _scoped_context()
    backend.contacts_connections = [
        contact_payload(
            resource_name="people/contact-x",
            display_name="X",
            emails=("xavier@example.com",),
        )
    ]
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(
            session,
            IdentityQuery(
                provider="google",
                provider_account_id="identity-sub",
                email="xavier@example.com",
            ),
            backend,
            task,
            account_id,
        )
    assert result.status is IdentityStatus.CONFLICTING


async def test_many_name_candidates_with_no_exact_identifier_is_conflicting() -> None:
    backend, tenant_id, user_id, account_id = await _scoped_context()
    backend.contacts_connections = [
        contact_payload(resource_name="people/j1", display_name="John Smith"),
        contact_payload(resource_name="people/j2", display_name="John Smith"),
    ]
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(
            session, IdentityQuery(name="John Smith"), backend, task, account_id
        )
    assert result.status is IdentityStatus.CONFLICTING


# ---------------------------------------------------------------------------
# 3. Isolation: the query can never move or widen the context scope
# ---------------------------------------------------------------------------


async def test_foreign_tenant_cannot_see_our_contacts() -> None:
    backend, tenant_id, user_id, account_id = await _scoped_context()
    backend.contacts_connections = [
        contact_payload(
            resource_name="people/alice", display_name="Alice", emails=("alice@example.com",)
        )
    ]
    other_tenant, other_user = await make_tenant_user("other@corp.example")
    task = await make_task(tenant_id=other_tenant.id, user_id=other_user.id)
    async with session_scope() as session:
        result = await _resolved(
            session, IdentityQuery(email="alice@example.com"), backend, task, account_id
        )
    assert result.status is IdentityStatus.UNKNOWN


async def test_foreign_user_in_same_tenant_cannot_see_our_account() -> None:
    backend, tenant_id, user_id, account_id = await _scoped_context()
    _sibling_tenant, sibling = await make_tenant_user("sibling@corp.example")
    task = await make_task(tenant_id=tenant_id, user_id=sibling.id)
    async with session_scope() as session:
        result = await _resolved(
            session,
            IdentityQuery(provider="google", provider_account_id="identity-sub"),
            backend,
            task,
            account_id,
        )
    assert result.status is IdentityStatus.UNKNOWN


async def test_foreign_provider_subject_does_not_verify() -> None:
    """A sub that belongs to a different account is not ours."""
    backend, tenant_id, user_id, account_id = await _scoped_context()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(
            session,
            IdentityQuery(provider="google", provider_account_id="someone-elses-sub"),
            backend,
            task,
            account_id,
        )
    assert result.status is IdentityStatus.UNKNOWN


async def test_contacts_load_failure_degrades_to_provider_evidence_only() -> None:
    """A 5xx from Google removes contact evidence but provider evidence stands."""
    backend, tenant_id, user_id, account_id = await _scoped_context()
    backend.contacts_status = 500
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(
            session,
            IdentityQuery(provider="google", provider_account_id="identity-sub"),
            backend,
            task,
            account_id,
        )
    assert result.status is IdentityStatus.PROVIDER_VERIFIED
    assert result.confidence == 0.9


async def test_contacts_load_failure_with_contact_only_query_is_unknown() -> None:
    backend, tenant_id, user_id, account_id = await _scoped_context()
    backend.contacts_status = 500
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    async with session_scope() as session:
        result = await _resolved(
            session,
            IdentityQuery(email="alice@example.com"),
            backend,
            task,
            account_id,
        )
    assert result.status is IdentityStatus.UNKNOWN


# ---------------------------------------------------------------------------
# 4. Gateway + Policy integration
# ---------------------------------------------------------------------------


async def test_gateway_resolves_verified_identity_for_medium_risk() -> None:
    """VERIFIED identity passes the identity rule; the risk rule still needs
    human approval — identity only answers *who*, never removes risk gates."""
    backend, tenant_id, user_id, account_id = await _scoped_context()
    task = await make_task(tenant_id=tenant_id, user_id=user_id, risk_policy="MEDIUM")
    gateway, parts = make_gateway(
        identity=IdentityResolverStub(status=IdentityStatus.PROVIDER_VERIFIED),
        approval=_blocking_approval(),
    )
    async with session_scope() as session:
        context = make_context(
            session=session,
            task=task,
            backend=backend,
            account_id=account_id,
            identity=IdentityQuery(provider="google", provider_account_id="identity-sub"),
        )
        result = await gateway.execute(_send_intent(task), context)
        await close_context(context)

    assert result.outcome is ToolOutcome.APPROVAL_REQUIRED
    assert not any(
        d.reason_code == "IDENTITY_CONFLICTING" for d in parts["policy"].decisions
    )
    assert parts["policy"].decisions[-1].reason_code == "APPROVAL_REQUIRED_BY_RISK"


async def test_gateway_denies_conflicting_identity_for_medium_risk() -> None:
    backend, tenant_id, user_id, account_id = await _scoped_context()
    task = await make_task(tenant_id=tenant_id, user_id=user_id, risk_policy="MEDIUM")
    stub_resolver = IdentityResolverStub(status=IdentityStatus.CONFLICTING)
    gateway, parts = make_gateway(
        identity=stub_resolver, approval=_blocking_approval()
    )
    async with session_scope() as session:
        context = make_context(
            session=session,
            task=task,
            backend=backend,
            account_id=account_id,
            identity=IdentityQuery(provider="google", provider_account_id="identity-sub"),
        )
        result = await gateway.execute(_send_intent(task), context)
        await close_context(context)

    assert result.outcome is ToolOutcome.DENIED
    assert parts["policy"].decisions[-1].reason_code == "IDENTITY_CONFLICTING"
    assert len(stub_resolver.calls) == 1


async def test_gateway_escalates_unknown_identity_for_medium_risk() -> None:
    backend, tenant_id, user_id, account_id = await _scoped_context()
    task = await make_task(tenant_id=tenant_id, user_id=user_id, risk_policy="MEDIUM")
    gateway, parts = make_gateway(
        identity=IdentityResolverStub(status=IdentityStatus.UNKNOWN),
        approval=_blocking_approval(),
    )
    async with session_scope() as session:
        context = make_context(
            session=session,
            task=task,
            backend=backend,
            account_id=account_id,
            identity=IdentityQuery(provider="google", provider_account_id="identity-sub"),
        )
        result = await gateway.execute(_send_intent(task), context)
        await close_context(context)

    assert result.outcome is ToolOutcome.APPROVAL_REQUIRED
    assert parts["policy"].decisions[-1].reason_code == "IDENTITY_UNKNOWN"


async def test_gateway_low_risk_is_unconstrained_by_identity() -> None:
    """Below MEDIUM risk, identity never blocks — there is nothing to be
    careful about yet."""
    backend, tenant_id, user_id, account_id = await _scoped_context()
    task = await make_task(tenant_id=tenant_id, user_id=user_id, risk_policy="LOW")
    gateway, parts = make_gateway(identity=IdentityResolverStub(status=IdentityStatus.UNKNOWN))
    async with session_scope() as session:
        context = make_context(
            session=session,
            task=task,
            backend=backend,
            account_id=account_id,
            identity=IdentityQuery(provider="google", provider_account_id="identity-sub"),
        )
        result = await gateway.execute(
            make_intent("contacts.match", task=task, arguments={"query": "Alice"}), context
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.EXECUTION_SUCCEEDED


async def test_gateway_without_resolver_uses_unknown_fallback() -> None:
    """A context that asks for identity resolution must never guess: no
    resolver wired means UNKNOWN, which for MEDIUM escalates to approval."""
    from kai.policy import PolicyEngine
    from kai.tools import (
        ContentAuthorizerStub,
        ContractVerificationEngine,
        ToolGateway,
        default_registry,
    )

    backend, tenant_id, user_id, account_id = await _scoped_context()
    task = await make_task(tenant_id=tenant_id, user_id=user_id, risk_policy="MEDIUM")
    pol = PolicyEngine()
    gateway = ToolGateway(
        registry=default_registry(),
        policy_engine=pol,
        content_authorizer=ContentAuthorizerStub(),
        approval_manager=_blocking_approval(),
        verification_engine=ContractVerificationEngine(),
        identity_resolver=None,
    )
    async with session_scope() as session:
        context = make_context(
            session=session,
            task=task,
            backend=backend,
            account_id=account_id,
            identity=IdentityQuery(provider="google", provider_account_id="identity-sub"),
        )
        result = await gateway.execute(_send_intent(task), context)
        await close_context(context)

    assert result.outcome is ToolOutcome.APPROVAL_REQUIRED
    assert pol.decisions[-1].reason_code == "IDENTITY_UNKNOWN"


async def test_gateway_without_identity_query_uses_legacy_metadata_path() -> None:
    """Without context.identity the legacy metadata path is used and the
    resolver is never invoked."""
    backend, tenant_id, user_id, account_id = await _scoped_context()
    task = await make_task(tenant_id=tenant_id, user_id=user_id, risk_policy="MEDIUM")
    stub_resolver = IdentityResolverStub(status=IdentityStatus.CONFLICTING)
    gateway, _ = make_gateway(identity=stub_resolver, approval=_blocking_approval())
    async with session_scope() as session:
        context = make_context(
            session=session,
            task=task,
            backend=backend,
            account_id=account_id,
        )
        result = await gateway.execute(_send_intent(task), context)
        await close_context(context)

    assert stub_resolver.calls == []
    assert result.outcome is ToolOutcome.APPROVAL_REQUIRED
