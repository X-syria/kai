"""Task/tenant/user isolation at the Tool Gateway.

The gateway authenticates every invocation through the *trusted context* —
the task must resolve under ``(tenant_id, user_id)`` and the intent's identity
must match the context. A forged intent cannot name a different tenant/user/
task than the one the system vouches for, and a successful gateway run never
mutates the task row.
"""

from __future__ import annotations

import uuid

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


async def _registered_tenant_user() -> tuple[uuid.UUID, uuid.UUID]:
    tenant, user = await make_tenant_user("isolation@corp.example")
    return tenant.id, user.id


async def test_intent_cannot_spoof_a_different_tenant_in_context() -> None:
    """A valid task + context, but an intent claiming another tenant, is a
    CONTEXT_MISMATCH before any execution."""
    backend = GoogleBackend()
    tenant_id, user_id = await _registered_tenant_user()
    account = await insert_account(
        tenant_id=tenant_id, user_id=user_id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant_id, user_id=user_id)

    gateway, _ = make_gateway()
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        forged = make_intent(
            "contacts.match",
            task=task,
            tenant_id=uuid.uuid4(),
            arguments={"query": "Alice"},
        )
        result = await gateway.execute(forged, context)
        await close_context(context)

    assert result.outcome is ToolOutcome.REJECTED
    assert result.error_code == "CONTEXT_MISMATCH"
    assert backend.contacts_calls == []


async def test_intent_cannot_spoof_a_different_user_in_context() -> None:
    backend = GoogleBackend()
    tenant_id, user_id = await _registered_tenant_user()
    account = await insert_account(
        tenant_id=tenant_id, user_id=user_id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant_id, user_id=user_id)

    gateway, _ = make_gateway()
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        forged = make_intent(
            "contacts.match",
            task=task,
            user_id=uuid.uuid4(),
            arguments={"query": "Alice"},
        )
        result = await gateway.execute(forged, context)
        await close_context(context)

    assert result.outcome is ToolOutcome.REJECTED
    assert result.error_code == "CONTEXT_MISMATCH"
    assert backend.contacts_calls == []


async def test_foreign_tenant_task_does_not_resolve() -> None:
    """A context pointing at tenant B with tenant A's task id cannot find the
    task: the lookup is scoped by the trusted context's tenant."""
    backend = GoogleBackend()
    tenant_a, user_a = await make_tenant_user("acme@corp.example")
    tenant_b, user_b = await make_tenant_user("globex@corp.example")
    task_a = await make_task(tenant_id=tenant_a.id, user_id=user_a.id)

    gateway, _ = make_gateway()
    async with session_scope() as session:
        context = make_context(
            session=session,
            task=task_a,  # task belongs to tenant A...
            backend=backend,
            account_id=None,
            tenant_id=tenant_b.id,  # ...but the context vouches for tenant B
            user_id=user_b.id,
        )
        result = await gateway.execute(
            make_intent(
                "contacts.match", task=task_a, arguments={"query": "Alice"}
            ),
            context,
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.REQUIRES_ATTENTION
    assert result.error_code == "TASK_UNRESOLVED"
    assert backend.contacts_calls == []


async def test_foreign_user_cannot_use_a_sibling_tasks_context() -> None:
    """A task owned by another user in another tenant does not resolve either."""
    backend = GoogleBackend()
    _, user_a = await make_tenant_user("acme@corp.example")
    tenant_b, user_b = await make_tenant_user("globex@corp.example")
    task_b = await make_task(tenant_id=tenant_b.id, user_id=user_b.id)

    gateway, _ = make_gateway()
    async with session_scope() as session:
        context = make_context(
            session=session,
            task=task_b,
            backend=backend,
            account_id=None,
            user_id=user_a.id,  # different user entirely
        )
        result = await gateway.execute(
            make_intent(
                "contacts.match", task=task_b, arguments={"query": "Alice"}
            ),
            context,
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.REQUIRES_ATTENTION
    assert result.error_code == "TASK_UNRESOLVED"


async def test_missing_task_does_not_resolve() -> None:
    """A context pointing at a task that never existed yields TASK_UNRESOLVED."""
    backend = GoogleBackend()
    tenant_id, user_id = await _registered_tenant_user()
    task = await make_task(tenant_id=tenant_id, user_id=user_id)
    ghost = uuid.uuid4()

    gateway, _ = make_gateway()
    async with session_scope() as session:
        context = make_context(
            session=session,
            task=task,
            backend=backend,
            account_id=None,
            task_id=ghost,
        )
        intent = make_intent(
            "contacts.match", task=task, task_id=ghost, arguments={"query": "Alice"}
        )
        result = await gateway.execute(intent, context)
        await close_context(context)

    assert result.outcome is ToolOutcome.REQUIRES_ATTENTION
    assert result.error_code == "TASK_UNRESOLVED"
    assert backend.contacts_calls == []


async def test_gateway_run_never_mutates_the_task_row() -> None:
    """Execution is a read-only consumer of the task: no state transitions
    happen inside the gateway; those are the Task Manager's job."""
    from kai.tasks.service import get_task

    backend = GoogleBackend()
    tenant_id, user_id = await _registered_tenant_user()
    account = await insert_account(
        tenant_id=tenant_id, user_id=user_id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant_id, user_id=user_id, allowed_tools=["contacts"])

    gateway, _ = make_gateway()
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent("contacts.match", task=task, arguments={"query": "Alice"}),
            context,
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.EXECUTION_SUCCEEDED
    async with session_scope() as session:
        reloaded = await get_task(
            session,
            task_id=task.id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        assert reloaded.id == task.id
        assert reloaded.state == task.state  # CREATED, untouched
        assert reloaded.attempts == task.attempts
        assert reloaded.last_error is None