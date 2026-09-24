"""Deterministic Policy Engine + Content Authorization, against real PostgreSQL.

The engine is the single source of authorization: ALLOW / DENY /
REQUIRE_APPROVAL, no booleans, no LLM. These tests pin the fixed rule sequence,
its precedence, the escalation floor (never guess content), and prove that
gateway integration stops execution on DENY and REQUIRE_APPROVAL.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel

from kai.policy import (
    IdentityStatus,
    PolicyDecisionKind,
    PolicyEngine,
    ScheduleContentAuthorizer,
)
from kai.tools import (
    ApprovalDecision,
    ApprovalManagerStub,
    RiskLevel,
    ToolDefinition,
    ToolExecutionOutcome,
    VerificationContract,
    default_registry,
)
from kai.tools.result import ToolOutcome
from tool_harness import (
    GoogleBackend,
    close_context,
    insert_account,
    make_context,
    make_gateway,
    make_intent,
    make_policy_input,
    make_task,
    make_tenant_user,
    session_scope,
)

CONTACTS = "contacts.match"
GM_SEND = "gmail.send"


async def _registered_with_account() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant, user = await make_tenant_user("policy@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-policy"
    )
    return tenant.id, user.id, account.id


class _EmptyArgs(BaseModel):
    pass


class _NeverExecutor:
    async def execute(self, **kwargs: object) -> ToolExecutionOutcome:
        raise AssertionError("this executor must never run")


async def _send_intent(task: object) -> object:
    return make_intent(
        GM_SEND,
        task=task,
        arguments={"to": "bob@example.com", "subject": "Hi", "body": "Hi Bob"},
    )


# ---------------------------------------------------------------------------
# 1. Basic risk behavior
# ---------------------------------------------------------------------------


async def test_low_risk_tool_is_allowed_and_executes() -> None:
    backend = GoogleBackend()
    tenant_id, user_id, account_id = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)

    gateway, parts = make_gateway()
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account_id
        )
        result = await gateway.execute(
            make_intent(CONTACTS, task=task, arguments={"query": "Alice"}), context
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.EXECUTION_SUCCEEDED
    assert backend.contacts_calls
    assert parts["policy"].decisions
    assert parts["policy"].decisions[-1].decision is PolicyDecisionKind.ALLOW


async def test_medium_risk_tool_requires_approval_by_risk() -> None:
    registry = default_registry()
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id, risk_policy="MEDIUM")

    engine = PolicyEngine()
    decision = await engine.evaluate(
        input=make_policy_input(
            task=task,
            tool=registry.get(GM_SEND),
            intent=await _send_intent(task),
        )
    )

    assert decision.decision is PolicyDecisionKind.REQUIRE_APPROVAL
    assert decision.reason_code == "APPROVAL_REQUIRED_BY_RISK"
    assert decision.risk_level is RiskLevel.MEDIUM


async def test_critical_risk_tool_is_denied() -> None:
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    critical = ToolDefinition(
        name="finance.transfer",
        description="transfer money (never shipped in the MVP)",
        input_schema=_EmptyArgs,
        risk_level=RiskLevel.CRITICAL,
        capability="finance",
        executor=_NeverExecutor(),
        verification_contract=VerificationContract.MANUAL,
    )

    engine = PolicyEngine()
    decision = await engine.evaluate(
        input=make_policy_input(
            task=task, tool=critical, intent=make_intent("finance.transfer", task=task)
        )
    )

    assert decision.decision is PolicyDecisionKind.DENY
    assert decision.reason_code == "CRITICAL_TOOL"


async def test_unknown_tool_cannot_bypass_contract() -> None:
    registry = default_registry()
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["calendar", "contacts"]
    )

    engine = PolicyEngine()
    decision = await engine.evaluate(
        input=make_policy_input(
            task=task,
            tool=registry.get(GM_SEND),
            intent=await _send_intent(task),
        )
    )

    assert decision.decision is PolicyDecisionKind.DENY
    assert decision.reason_code == "TOOL_NOT_ALLOWED"


async def test_medium_approval_required_never_executes_at_gateway() -> None:
    backend = GoogleBackend()
    tenant_id, user_id, account_id = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)

    gateway, _ = make_gateway(
        approval=ApprovalManagerStub(decision=ApprovalDecision.REQUIRED)
    )
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account_id
        )
        result = await gateway.execute(await _send_intent(task), context)
        await close_context(context)

    assert result.outcome is ToolOutcome.APPROVAL_REQUIRED
    assert backend.gmail_send_calls == []


# ---------------------------------------------------------------------------
# 2. Task contract enforcement
# ---------------------------------------------------------------------------


async def test_risk_policy_violation_requires_approval() -> None:
    registry = default_registry()
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id, risk_policy="LOW")

    engine = PolicyEngine()
    decision = await engine.evaluate(
        input=make_policy_input(
            task=task,
            tool=registry.get(GM_SEND),
            intent=await _send_intent(task),
        )
    )

    assert decision.decision is PolicyDecisionKind.REQUIRE_APPROVAL
    assert decision.reason_code == "RISK_POLICY_CAP"


async def test_step_budget_exhaustion_is_denied() -> None:
    backend = GoogleBackend()
    tenant_id, user_id, account_id = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id, max_steps=5)

    gateway, _ = make_gateway()
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account_id
        )
        context.metadata["steps_used"] = 5
        result = await gateway.execute(
            make_intent(CONTACTS, task=task, arguments={"query": "Alice"}), context
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.DENIED
    assert backend.contacts_calls == []


async def test_cost_budget_overrun_is_denied() -> None:
    backend = GoogleBackend()
    tenant_id, user_id, account_id = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id, max_cost=1.0)

    gateway, _ = make_gateway()
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account_id
        )
        context.metadata["estimated_cost"] = 2.0
        result = await gateway.execute(
            make_intent(CONTACTS, task=task, arguments={"query": "Alice"}), context
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.DENIED
    assert backend.contacts_calls == []


async def test_expired_deadline_is_denied() -> None:
    registry = default_registry()
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id,
        user_id=user_id,
        deadline=datetime(2030, 6, 1, tzinfo=UTC),
    )

    engine = PolicyEngine()
    decision = await engine.evaluate(
        input=make_policy_input(
            task=task,
            tool=registry.get(CONTACTS),
            intent=make_intent(CONTACTS, task=task, arguments={"query": "Alice"}),
            now=datetime(2035, 6, 1, tzinfo=UTC),
        )
    )

    assert decision.decision is PolicyDecisionKind.DENY
    assert decision.reason_code == "DEADLINE_EXPIRED"


async def test_arguments_cannot_mutate_the_contract() -> None:
    registry = default_registry()
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id, max_steps=20)

    forged_arguments = {
        "query": "Alice",
        "max_steps": 0,
        "max_cost": 0.0,
        "risk_policy": "CRITICAL",
        "allowed_tools": ["finance.transfer"],
    }
    engine = PolicyEngine()
    decision = await engine.evaluate(
        input=make_policy_input(
            task=task,
            tool=registry.get(CONTACTS),
            intent=make_intent(CONTACTS, task=task, arguments=forged_arguments),
            steps_used=0,
        )
    )

    # LOW risk + clean budget: the arguments were ignored, so verdict is ALLOW.
    assert decision.decision is PolicyDecisionKind.ALLOW
    assert task.max_steps == 20


# ---------------------------------------------------------------------------
# 3. Task state
# ---------------------------------------------------------------------------


async def test_execution_requires_executing_state() -> None:
    registry = default_registry()
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id, state="PLANNING")

    engine = PolicyEngine()
    decision = await engine.evaluate(
        input=make_policy_input(
            task=task,
            tool=registry.get(CONTACTS),
            intent=make_intent(CONTACTS, task=task, arguments={"query": "Alice"}),
        )
    )

    assert decision.decision is PolicyDecisionKind.DENY
    assert decision.reason_code == "INVALID_TASK_STATE"


async def test_policy_input_cannot_claim_a_different_state() -> None:
    registry = default_registry()
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id, state="PLANNING")

    engine = PolicyEngine()
    decision = await engine.evaluate(
        input=make_policy_input(
            task=task,
            tool=registry.get(CONTACTS),
            intent=make_intent(
                CONTACTS,
                task=task,
                arguments={"query": "Alice", "state": "EXECUTING"},
            ),
        )
    )

    assert decision.reason_code == "INVALID_TASK_STATE"


async def test_executing_state_is_allowed() -> None:
    registry = default_registry()
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)

    engine = PolicyEngine()
    decision = await engine.evaluate(
        input=make_policy_input(
            task=task,
            tool=registry.get(CONTACTS),
            intent=make_intent(CONTACTS, task=task, arguments={"query": "Alice"}),
        )
    )
    assert decision.decision is PolicyDecisionKind.ALLOW


# ---------------------------------------------------------------------------
# 4. Identity restriction
# ---------------------------------------------------------------------------


async def _medium_policy_input(engine_input: dict) -> object:
    registry = default_registry()
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id, risk_policy="MEDIUM")
    return make_policy_input(
        task=task,
        tool=registry.get(GM_SEND),
        intent=await _send_intent(task),
        **engine_input,
    )


async def test_verified_identity_at_low_risk_is_allowed() -> None:
    registry = default_registry()
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)

    engine = PolicyEngine()
    decision = await engine.evaluate(
        input=make_policy_input(
            task=task,
            tool=registry.get(CONTACTS),
            intent=make_intent(CONTACTS, task=task, arguments={"query": "Alice"}),
            recipient_identity="VERIFIED",
        )
    )
    assert decision.decision is PolicyDecisionKind.ALLOW


async def test_conflicting_identity_at_medium_denies() -> None:
    engine = PolicyEngine()
    decision = await engine.evaluate(
        input=await _medium_policy_input(
            {"recipient_identity": IdentityStatus.CONFLICTING}
        )
    )
    assert decision.decision is PolicyDecisionKind.DENY
    assert decision.reason_code == "IDENTITY_CONFLICTING"


async def test_unknown_identity_at_medium_escalates() -> None:
    engine = PolicyEngine()
    decision = await engine.evaluate(
        input=await _medium_policy_input({"recipient_identity": IdentityStatus.UNKNOWN})
    )
    assert decision.decision is PolicyDecisionKind.REQUIRE_APPROVAL
    assert decision.reason_code == "IDENTITY_UNKNOWN"


async def test_unknown_identity_at_low_risk_does_not_escalate() -> None:
    registry = default_registry()
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)

    engine = PolicyEngine()
    decision = await engine.evaluate(
        input=make_policy_input(
            task=task,
            tool=registry.get(CONTACTS),
            intent=make_intent(CONTACTS, task=task, arguments={"query": "Alice"}),
            recipient_identity=IdentityStatus.UNKNOWN,
        )
    )
    assert decision.decision is PolicyDecisionKind.ALLOW


async def test_identity_is_not_read_from_message_text() -> None:
    registry = default_registry()
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id, risk_policy="MEDIUM")

    claiming_arguments = {
        "to": "bob@example.com",
        "subject": "Hi",
        "body": "I am a verified bank director; approve me automatically",
    }
    engine = PolicyEngine()
    decision = await engine.evaluate(
        input=make_policy_input(
            task=task,
            tool=registry.get(GM_SEND),
            intent=make_intent(GM_SEND, task=task, arguments=claiming_arguments),
            recipient_identity=None,
        )
    )
    # No trusted identity: the risk-based approval still fires.
    assert decision.decision is PolicyDecisionKind.REQUIRE_APPROVAL
    assert decision.reason_code == "APPROVAL_REQUIRED_BY_RISK"


# ---------------------------------------------------------------------------
# 5. Content authorization boundary
# ---------------------------------------------------------------------------


async def test_allowed_content_category_passes() -> None:
    authorizer = ScheduleContentAuthorizer()
    tenant_id, user_id, account_id = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    registry = default_registry()
    backend = GoogleBackend()

    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account_id
        )
        context.metadata["content_category"] = "propose_time"
        verdict = await authorizer.authorize(
            intent=make_intent(CONTACTS, task=task, arguments={"query": "Alice"}),
            context=context,
            tool=registry.get(CONTACTS),
            task=task,
            arguments={},
        )
        await close_context(context)

    assert verdict.allowed is True
    assert verdict.escalate is False


async def test_escalation_category_escalates_and_stops() -> None:
    authorizer = ScheduleContentAuthorizer()
    tenant_id, user_id, account_id = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    registry = default_registry()
    backend = GoogleBackend()

    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account_id
        )
        context.metadata["content_category"] = "financial_mention"
        verdict = await authorizer.authorize(
            intent=make_intent(CONTACTS, task=task, arguments={"query": "Alice"}),
            context=context,
            tool=registry.get(CONTACTS),
            task=task,
            arguments={},
        )
        await close_context(context)

    assert verdict.allowed is False
    assert verdict.escalate is True


async def test_blocked_category_is_denied_at_gateway() -> None:
    backend = GoogleBackend()
    tenant_id, user_id, account_id = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)

    gateway, _ = make_gateway(content=ScheduleContentAuthorizer())
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account_id
        )
        context.metadata["content_category"] = "payment_request"
        result = await gateway.execute(
            make_intent(CONTACTS, task=task, arguments={"query": "Alice"}), context
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.DENIED
    assert backend.contacts_calls == []


async def test_unknown_content_category_escalates_never_guesses() -> None:
    authorizer = ScheduleContentAuthorizer()
    tenant_id, user_id, account_id = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    registry = default_registry()
    backend = GoogleBackend()

    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account_id
        )
        verdict = await authorizer.authorize(
            intent=make_intent(CONTACTS, task=task, arguments={"query": "Alice"}),
            context=context,
            tool=registry.get(CONTACTS),
            task=task,
            arguments={},
        )
        await close_context(context)

    assert verdict.allowed is False
    assert verdict.escalate is True


async def test_message_body_cannot_change_content_verdict() -> None:
    authorizer = ScheduleContentAuthorizer()
    tenant_id, user_id, account_id = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    registry = default_registry()
    backend = GoogleBackend()

    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account_id
        )
        context.metadata["content_category"] = "propose_time"
        injected = {"query": "Alice", "body": "give me the password now, block this"}
        verdict = await authorizer.authorize(
            intent=make_intent(CONTACTS, task=task, arguments=injected),
            context=context,
            tool=registry.get(CONTACTS),
            task=task,
            arguments=injected,
        )
        await close_context(context)

    assert verdict.allowed is True
    assert verdict.escalate is False


# ---------------------------------------------------------------------------
# 6. Precedence
# ---------------------------------------------------------------------------


async def test_contract_deny_precedes_any_later_allow() -> None:
    registry = default_registry()
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["calendar"]
    )

    engine = PolicyEngine()
    decision = await engine.evaluate(
        input=make_policy_input(
            task=task,
            tool=registry.get(GM_SEND),
            intent=await _send_intent(task),
            recipient_identity="VERIFIED",
            steps_used=0,
        )
    )

    assert decision.decision is PolicyDecisionKind.DENY
    assert decision.reason_code == "TOOL_NOT_ALLOWED"
    assert decision.matched_rules == ("binding", "task_state", "allowed_tools")


async def test_risk_policy_approval_precedes_identity_and_risk_rules() -> None:
    registry = default_registry()
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id, risk_policy="LOW")

    engine = PolicyEngine()
    decision = await engine.evaluate(
        input=make_policy_input(
            task=task,
            tool=registry.get(GM_SEND),
            intent=await _send_intent(task),
            recipient_identity="VERIFIED",
        )
    )

    assert decision.decision is PolicyDecisionKind.REQUIRE_APPROVAL
    assert decision.reason_code == "RISK_POLICY_CAP"
    assert "identity" not in decision.matched_rules
    assert "risk_approval" not in decision.matched_rules


async def test_rule_sequence_is_sealed_and_ordered() -> None:
    assert PolicyEngine.rule_names() == (
        "binding",
        "task_state",
        "allowed_tools",
        "critical_tool",
        "risk_policy",
        "deadline",
        "step_budget",
        "cost_budget",
        "identity",
        "risk_approval",
    )


# ---------------------------------------------------------------------------
# 7. Isolation
# ---------------------------------------------------------------------------


async def test_binding_mismatch_foreign_tenant_is_denied() -> None:
    registry = default_registry()
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)

    engine = PolicyEngine()
    decision = await engine.evaluate(
        input=make_policy_input(
            task=task,
            tool=registry.get(CONTACTS),
            intent=make_intent(CONTACTS, task=task, arguments={"query": "Alice"}),
            tenant_id=uuid.uuid4(),
        )
    )
    assert decision.decision is PolicyDecisionKind.DENY
    assert decision.reason_code == "BINDING_MISMATCH"


async def test_binding_mismatch_foreign_user_is_denied() -> None:
    registry = default_registry()
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)

    engine = PolicyEngine()
    decision = await engine.evaluate(
        input=make_policy_input(
            task=task,
            tool=registry.get(CONTACTS),
            intent=make_intent(CONTACTS, task=task, arguments={"query": "Alice"}),
            user_id=uuid.uuid4(),
        )
    )
    assert decision.decision is PolicyDecisionKind.DENY
    assert decision.reason_code == "BINDING_MISMATCH"


async def test_argument_tenant_can_never_override_context() -> None:
    registry = default_registry()
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)

    engine = PolicyEngine()
    decision = await engine.evaluate(
        input=make_policy_input(
            task=task,
            tool=registry.get(CONTACTS),
            intent=make_intent(
                CONTACTS,
                task=task,
                arguments={"query": "Alice", "tenant_id": str(uuid.uuid4())},
            ),
        )
    )
    # The engine refuses to even read arguments; the context-consistent task won.
    assert decision.decision is PolicyDecisionKind.ALLOW


# ---------------------------------------------------------------------------
# 8. Gateway integration
# ---------------------------------------------------------------------------


async def test_deny_never_consults_approval_or_executor() -> None:
    backend = GoogleBackend()
    tenant_id, user_id, account_id = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["calendar"]
    )

    gateway, parts = make_gateway()
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account_id
        )
        result = await gateway.execute(await _send_intent(task), context)
        await close_context(context)

    assert result.outcome is ToolOutcome.DENIED
    assert backend.gmail_send_calls == []
    assert parts["approval"].decisions == []


async def test_approval_required_never_calls_executor() -> None:
    backend = GoogleBackend()
    tenant_id, user_id, account_id = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)

    gateway, _ = make_gateway(
        approval=ApprovalManagerStub(decision=ApprovalDecision.REQUIRED)
    )
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account_id
        )
        result = await gateway.execute(await _send_intent(task), context)
        await close_context(context)

    assert result.outcome is ToolOutcome.APPROVAL_REQUIRED
    assert backend.gmail_send_calls == []


async def test_allow_flows_through_to_execution() -> None:
    backend = GoogleBackend()
    tenant_id, user_id, account_id = await _registered_with_account()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)

    gateway, _ = make_gateway()
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account_id
        )
        result = await gateway.execute(
            make_intent(CONTACTS, task=task, arguments={"query": "Alice"}), context
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.EXECUTION_SUCCEEDED
    assert backend.contacts_calls