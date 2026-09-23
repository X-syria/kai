"""Tenant-scoped Task operations.

Every lookup is scoped by ``WHERE tenant_id = ?`` (plus ``user_id`` where the
caller is a specific user), so a task id from another tenant or another user
simply does not resolve. State changes go through the explicit machine in
:mod:`kai.tasks.state`; anything the machine refuses raises
:class:`InvalidTransition` and the row is left untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kai.tasks.models import Task
from kai.tasks.schemas import TaskContractInput
from kai.tasks.state import (
    CREATED,
    RETRY,
    InvalidTransition,
    apply_retry_budget,
    is_retryable_failure,
    is_terminal,
    resolve_transition,
)


class TaskNotFoundError(ValueError):
    """Raised when no task resolves for the requested ``(tenant, user, id)``."""


async def create_task(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    contract: TaskContractInput,
) -> Task:
    """Persist a freshly ``CREATED`` task for one tenant/user."""
    task = Task(
        tenant_id=tenant_id,
        user_id=user_id,
        state=CREATED,
        goal=contract.goal,
        success_criteria=list(contract.success_criteria),
        allowed_tools=list(contract.allowed_tools),
        risk_policy=contract.risk_policy,
        deadline=contract.deadline,
        max_cost=contract.max_cost,
        max_steps=contract.max_steps,
        verification_method=contract.verification_method,
        fallback=contract.fallback,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


def _task_scope(task_id: UUID, *, tenant_id: UUID, user_id: UUID | None) -> Select:
    stmt = select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id)
    if user_id is not None:
        stmt = stmt.where(Task.user_id == user_id)
    return stmt


async def _load_task(
    session: AsyncSession,
    *,
    task_id: UUID,
    tenant_id: UUID,
    user_id: UUID | None,
) -> Task:
    result = await session.execute(
        _task_scope(task_id, tenant_id=tenant_id, user_id=user_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise TaskNotFoundError("task not found for tenant")
    return task


async def get_task(
    session: AsyncSession,
    *,
    task_id: UUID,
    tenant_id: UUID,
    user_id: UUID | None = None,
) -> Task:
    """Fetch one task scoped to ``tenant_id`` (and ``user_id`` when given)."""
    return await _load_task(
        session, task_id=task_id, tenant_id=tenant_id, user_id=user_id
    )


async def list_tasks(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID | None = None,
    include_archived: bool = False,
) -> list[Task]:
    """List tasks for the tenant, newest first; callers own the relationship.

    ``user_id`` narrows the listing to one user. ``include_archived`` adds the
    tombstones that the default view hides.
    """
    stmt = select(Task).where(Task.tenant_id == tenant_id)
    if user_id is not None:
        stmt = stmt.where(Task.user_id == user_id)
    if not include_archived:
        stmt = stmt.where(Task.is_archived.is_(False))
    stmt = stmt.order_by(Task.created_at.desc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def transition_task(
    session: AsyncSession,
    *,
    task_id: UUID,
    tenant_id: UUID,
    user_id: UUID | None,
    target: str,
    fail_reason: str | None = None,
) -> Task:
    """Move a task to ``target`` through the explicit machine.

    Legal transitions update the row; the machine also enforces the retry
    budget on the failure -> retry path. Anything else raises
    :class:`InvalidTransition` and persists nothing.
    """
    task = await _load_task(
        session, task_id=task_id, tenant_id=tenant_id, user_id=user_id
    )
    decision = resolve_transition(task.state, target)
    if not decision.ok or decision.next_state is None:
        raise InvalidTransition(decision.error or "transition refused")

    next_state = decision.next_state
    if is_retryable_failure(task.state) and next_state == RETRY:
        budget = apply_retry_budget(
            current=task.state,
            target=next_state,
            attempts=task.attempts,
            max_retries=task.max_retries,
        )
        if not budget.ok or budget.next_state is None:
            raise InvalidTransition(budget.error or "retry refused")
        next_state = budget.next_state

    task.state = next_state
    if next_state == RETRY:
        task.attempts += 1
        task.failed_at = None
        task.last_error = None
    elif next_state in ("FAILED", "REQUIRES_ATTENTION"):
        task.failed_at = datetime.now(UTC)
    elif is_terminal(next_state):
        task.failed_at = None

    if fail_reason and next_state in ("FAILED", "REQUIRES_ATTENTION"):
        task.last_error = " ".join(fail_reason.split())[:2000]

    await session.commit()
    await session.refresh(task)
    return task


async def archive_task(
    session: AsyncSession,
    *,
    task_id: UUID,
    tenant_id: UUID,
    user_id: UUID | None,
) -> Task:
    """Hide a task from the default list view (only terminal tasks)."""
    task = await _load_task(
        session, task_id=task_id, tenant_id=tenant_id, user_id=user_id
    )
    if not is_terminal(task.state):
        raise InvalidTransition("only terminal tasks can be archived")
    task.is_archived = True
    await session.commit()
    await session.refresh(task)
    return task


async def assign_task(
    session: AsyncSession,
    *,
    task_id: UUID,
    tenant_id: UUID,
    user_id: UUID,
) -> Task:
    """Re-bind a task to a user *within the same tenant*.

    This is a test-only seam so a tenant can own more than one user (the MVP
    registers one tenant == one user). It never crosses tenants: the target
    tenant_id is validated first.
    """
    task = await _load_task(
        session, task_id=task_id, tenant_id=tenant_id, user_id=None
    )
    task.user_id = user_id
    await session.commit()
    await session.refresh(task)
    return task


async def count_tasks(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID | None = None,
) -> int:
    """Count tasks visible to the tenant scope (state leakage test helper)."""
    stmt = select(func.count(Task.id)).where(Task.tenant_id == tenant_id)
    if user_id is not None:
        stmt = stmt.where(Task.user_id == user_id)
    result = await session.execute(stmt)
    return int(result.scalar_one())