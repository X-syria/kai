"""Tool Gateway pipeline behavior: boundaries run in a fixed order and
stop conditions are deterministic.

Everything here goes through a fully wired ``ToolGateway`` over real rows in
real PostgreSQL; only Google's network is served by ``httpx.MockTransport``.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from kai.tools import (
    ApprovalDecision,
    ApprovalManagerStub,
    ContentAuthorizerStub,
    RiskLevel,
    ToolDefinition,
    ToolExecutionOutcome,
    ToolRegistry,
    VerificationContract,
)
from kai.tools.result import ToolOutcome
from tool_harness import (
    GoogleBackend,
    close_context,
    insert_account,
    make_context,
    make_gateway,
    make_intent,
    make_task,
    make_tenant_user,
    session_scope,
)


async def test_low_risk_tool_passes_boundaries_and_executes() -> None:
    """contacts.match (LOW) must clear policy, run content auth, and, being
    below the approval threshold, execute without any approval consult."""
    backend = GoogleBackend()
    backend.contacts_connections = [
        {
            "resourceName": "people/abc",
            "names": [{"displayName": "Alice Example"}],
            "emailAddresses": [{"value": "alice@example.com"}],
            "phoneNumbers": [{"value": "+1 555 0100"}],
        }
    ]
    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id)

    gateway, parts = make_gateway(
        approval=ApprovalManagerStub(decision=ApprovalDecision.REQUIRED)
    )
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent(
                "contacts.match",
                task=task,
                arguments={"query": "Alice"},
            ),
            context,
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.EXECUTION_SUCCEEDED
    assert result.ok is True
    assert result.verified is True
    assert result.data == [
        {
            "resource_name": "people/abc",
            "display_name": "Alice Example",
            "emails": ["alice@example.com"],
            "phone_numbers": ["+1 555 0100"],
        }
    ]
    # Policy and content boundaries were consulted...
    assert parts["policy"].decisions
    assert parts["content"].calls
    # ...but the approval manager was NOT consulted for a LOW tool.
    approval: ApprovalManagerStub = parts["approval"]
    assert approval.decisions == []


async def test_medium_tool_with_required_approval_never_executes() -> None:
    """gmail.send (MEDIUM) with REQUIRED approval must stop at the approval
    boundary: no provider call, no send, and a structured APPROVAL_REQUIRED."""
    backend = GoogleBackend()
    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id)

    gateway, parts = make_gateway(
        approval=ApprovalManagerStub(decision=ApprovalDecision.REQUIRED)
    )
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent(
                "gmail.send",
                task=task,
                arguments={
                    "to": "bob@example.com",
                    "subject": "Hello Bob",
                    "body": "Hi Bob",
                },
            ),
            context,
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.APPROVAL_REQUIRED
    assert result.ok is False
    assert result.verified is False
    assert backend.gmail_send_calls == []
    approval: ApprovalManagerStub = parts["approval"]
    assert len(approval.decisions) == 1


async def test_denied_approval_stops_execution() -> None:
    backend = GoogleBackend()
    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id)

    gateway, _ = make_gateway(
        approval=ApprovalManagerStub(decision=ApprovalDecision.DENIED)
    )
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent(
                "gmail.send",
                task=task,
                arguments={
                    "to": "bob@example.com",
                    "subject": "Hello Bob",
                    "body": "Hi Bob",
                },
            ),
            context,
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.DENIED
    assert backend.gmail_send_calls == []
    assert result.error_code is None


async def test_content_boundary_deny_stops_before_execution() -> None:
    backend = GoogleBackend()
    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id)

    gateway, parts = make_gateway(content=ContentAuthorizerStub(allowed=False))
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent(
                "contacts.match", task=task, arguments={"query": "Alice"}
            ),
            context,
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.DENIED
    assert backend.contacts_calls == []
    assert parts["content"].calls


async def test_policy_denies_tools_outside_the_task_contract() -> None:
    """The task contract's allowed_tools wins over whatever the intent named."""
    backend = GoogleBackend()
    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    # Contract permits calendar and contacts, NOT gmail.
    task = await make_task(
        tenant_id=tenant.id, user_id=user.id, allowed_tools=["calendar", "contacts"]
    )

    gateway, parts = make_gateway()
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent(
                "gmail.send",
                task=task,
                arguments={
                    "to": "bob@example.com",
                    "subject": "Hello Bob",
                    "body": "Hi Bob",
                },
            ),
            context,
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.DENIED
    assert "does not permit capability" in result.message
    assert backend.gmail_send_calls == []
    assert parts["policy"].decisions


async def test_account_required_guard_returns_attention() -> None:
    """A tool that needs an account, without one in context, never executes."""
    backend = GoogleBackend()
    tenant, user = await make_tenant_user("acme@corp.example")
    task = await make_task(tenant_id=tenant.id, user_id=user.id)

    gateway, _ = make_gateway()
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=None
        )
        result = await gateway.execute(
            make_intent(
                "gmail.search",
                task=task,
                arguments={"query": "from:alice", "max_results": 5},
            ),
            context,
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.REQUIRES_ATTENTION
    assert result.error_code == "ACCOUNT_REQUIRED"
    assert backend.gmail_search_calls == []


class _CriticalArgs(BaseModel):
    pass


async def test_critical_risk_tool_is_denied_by_mvp_policy() -> None:
    """CRITICAL capabilities do not exist in the MVP catalog; even when
    someone wires a CRITICAL definition in, the policy refuses it."""
    backend = GoogleBackend()
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="finance.transfer",
            description="transfer money (never shipped in the MVP)",
            input_schema=_CriticalArgs,
            risk_level=RiskLevel.CRITICAL,
            capability="finance",
            executor=NeverRunsExecutor(),
            verification_contract=VerificationContract.MANUAL,
        )
    )
    registry.lock()
    tenant, user = await make_tenant_user("acme@corp.example")
    task = await make_task(
        tenant_id=tenant.id, user_id=user.id, allowed_tools=["finance"]
    )

    gateway, parts = make_gateway(registry=registry)
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=None
        )
        result = await gateway.execute(
            make_intent("finance.transfer", task=task, arguments={}), context
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.DENIED
    assert "CRITICAL" in result.message
    assert parts["policy"].decisions


async def test_missing_required_argument_is_rejected() -> None:
    backend = GoogleBackend()
    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id)
    gateway, _ = make_gateway()
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent("contacts.match", task=task, arguments={}), context
        )
        await close_context(context)
    assert result.outcome is ToolOutcome.REJECTED
    assert result.error_code == "VALIDATION_ERROR"
    assert backend.contacts_calls == []


async def test_extra_unknown_argument_is_rejected_extra_forbid() -> None:
    backend = GoogleBackend()
    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id)
    gateway, _ = make_gateway()
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent(
                "contacts.match",
                task=task,
                arguments={"query": "Alice", "unexpected_key": "x"},
            ),
            context,
        )
        await close_context(context)
    assert result.outcome is ToolOutcome.REJECTED
    assert result.error_code == "VALIDATION_ERROR"
    assert "unexpected" in result.message.lower()
    assert backend.contacts_calls == []


async def test_wrong_type_argument_is_rejected() -> None:
    backend = GoogleBackend()
    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id)
    gateway, _ = make_gateway()
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent(
                "contacts.match",
                task=task,
                arguments={"query": 42},
            ),
            context,
        )
        await close_context(context)
    assert result.outcome is ToolOutcome.REJECTED
    assert result.error_code == "VALIDATION_ERROR"


async def test_reserved_system_key_in_arguments_is_rejected() -> None:
    backend = GoogleBackend()
    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id)
    gateway, _ = make_gateway()
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent(
                "contacts.match",
                task=task,
                arguments={
                    "query": "Alice",
                    "tenant_id": str(uuid.uuid4()),
                },
            ),
            context,
        )
        await close_context(context)
    assert result.outcome is ToolOutcome.REJECTED
    assert result.error_code == "RESERVED_ARGUMENT"
    assert backend.contacts_calls == []


async def test_invalid_timezone_is_rejected() -> None:
    backend = GoogleBackend()
    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id)
    gateway, _ = make_gateway()
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent(
                "calendar.read_slots",
                task=task,
                arguments={"date": "2026-06-24", "timezone": "Mars/Olympus"},
            ),
            context,
        )
        await close_context(context)
    assert result.outcome is ToolOutcome.REJECTED
    assert result.error_code == "VALIDATION_ERROR"
    assert backend.calendar_calls == []


async def test_header_injection_in_subject_is_rejected_by_schema() -> None:
    backend = GoogleBackend()
    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id)
    gateway, _ = make_gateway(approval=ApprovalManagerStub())
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent(
                "gmail.send",
                task=task,
                arguments={
                    "to": "bob@example.com",
                    "subject": "Hello\r\nBcc: evil@example.com",
                    "body": "Hi Bob",
                },
            ),
            context,
        )
        await close_context(context)
    assert result.outcome is ToolOutcome.REJECTED
    assert result.error_code == "VALIDATION_ERROR"
    assert backend.gmail_send_calls == []


class NeverRunsExecutor:
    async def execute(self, **kwargs: object) -> ToolExecutionOutcome:
        raise AssertionError("this executor must never run")