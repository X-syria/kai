"""Tenant-scoped Task operations.

Every lookup is scoped by ``WHERE tenant_id = ?`` (plus ``user_id`` where the
caller is a specific user), so a task id from another tenant or another user
simply does not resolve. State changes go through the explicit machine in
:mod:`kai.tasks.state`; anything the machine refuses raises
:class:`InvalidTransition` and the row is left untouched.

Who may *ask* for a state is a separate question from whether the machine
allows it: the runtime asserts the lifecycle states from real evidence, and an
external client may only ask for the ones in
:data:`CLIENT_TRANSITION_TARGETS` (see :class:`InvalidActorError`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kai.tasks.models import Task
from kai.tasks.schemas import FulfillmentProfile, TaskContractInput
from kai.tasks.state import (
    CREATED,
    FAILED,
    REQUIRES_ATTENTION,
    RETRY,
    InvalidTransition,
    apply_retry_budget,
    is_retryable_failure,
    is_terminal,
    resolve_transition,
)

#: The runtime, the approvals flow, and every other in-process caller: the
#: things that establish a lifecycle state from real evidence.
INTERNAL_ACTOR: Final[str] = "internal"
#: An authenticated API caller acting for itself.
EXTERNAL_ACTOR: Final[str] = "client"


class TaskNotFoundError(ValueError):
    """Raised when no task resolves for the requested ``(tenant, user, id)``."""


#: The states an external client is allowed to move its own task into, and the
#: rest of the lifecycle it may not. ``FAILED``/``REQUIRES_ATTENTION`` are the
#: honest answers a client can give about its own request - "I am not paying for
#: this" and "this needs a human" - and the machine accepts them from any
#: non-terminal state. ``RETRY`` re-enters the lifecycle and spends the budget.
#:
#: Everything else in :data:`kai.tasks.state.ALL_STATES` is asserted by the
#: runtime from real evidence: that the planner produced a plan, that the policy
#: engine cleared the task, that a tool call actually returned, that the
#: success criteria are actually satisfied. The machine allows those edges, but
#: a client is not the thing that establishes the fact, so letting it name them
#: would let a caller record work that never happened - including declaring
#: ``COMPLETED`` on a task whose calendar event does not exist.
CLIENT_TRANSITION_TARGETS: frozenset[str] = frozenset(
    {FAILED, REQUIRES_ATTENTION, RETRY}
)


#: The set of fulfillment profiles a contract may be sealed with for live
#: execution (§10.10). Membership in :data:`FulfillmentProfile` is not itself
#: permission to seal: a listed profile must carry at least one criterion value
#: that its evaluator decides on from a sealed user-intent source. ``gmail_send``
#: stays in the vocabulary but is not listed, so it remains sealable at the
#: schema layer and is refused here, at the single write to
#: ``Task.fulfillment_profile``.
LIVE_SEALABLE_PROFILES: frozenset[FulfillmentProfile] = frozenset({
    "calendar_meeting",
})


class InvalidActorError(ValueError):
    """Raised when a caller may not drive a state the machine would allow.

    Distinct from :class:`InvalidTransition`: the machine is not refusing the
    edge, the caller is not entitled to ask for it.
    """


class UnlicensedFulfillmentError(ValueError):
    """Raised when a contract names a profile that may not be sealed for live use.

    The value is a valid :data:`FulfillmentProfile` but is not in
    :data:`LIVE_SEALABLE_PROFILES`, so persisting it would grant a live-sealing
    licence the semantic review did not warrant (§10.10).
    """


async def create_task(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    contract: TaskContractInput,
) -> Task:
    """Persist a freshly ``CREATED`` task for one tenant/user.

    The sealing gate lives here, before the row is written: a contract may only
    be sealed with a profile from :data:`LIVE_SEALABLE_PROFILES`, so a profile
    the review did not licence for live execution is refused at the single write
    to ``Task.fulfillment_profile``.
    """
    if contract.fulfillment_profile not in LIVE_SEALABLE_PROFILES:
        raise UnlicensedFulfillmentError(
            f"fulfillment profile {contract.fulfillment_profile!r} "
            "is not licensed for live sealing"
        )
    task = Task(
        tenant_id=tenant_id,
        user_id=user_id,
        state=CREATED,
        goal=contract.goal,
        success_criteria=list(contract.success_criteria),
        fulfillment_profile=contract.fulfillment_profile,
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


async def _load_task_for_update(
    session: AsyncSession,
    *,
    task_id: UUID,
    tenant_id: UUID,
    user_id: UUID | None,
) -> Task:
    """Lock the task row and read it as it is right now.

    ``populate_existing`` because the deciding session may have loaded this task
    earlier in the same request: a lock that returned the session's own cached,
    possibly stale image would serialise the writers while still letting the
    loser decide against a state that no longer exists.
    """
    result = await session.execute(
        _task_scope(task_id, tenant_id=tenant_id, user_id=user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
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
    actor: str = INTERNAL_ACTOR,
) -> Task:
    """Move a task to ``target`` through the explicit machine.

    Legal transitions update the row; the machine also enforces the retry
    budget on the failure -> retry path. Anything else raises
    :class:`InvalidTransition` and persists nothing.

    The row is locked for the whole read-decide-write, so two writers cannot
    both pass the machine on the same state. Without the lock this is a lost
    update: a reader that never saw the approval pause moves
    ``EXECUTING -> VERIFYING`` and overwrites the writer that moved
    ``EXECUTING -> WAITING_APPROVAL``, so a task completes having never asked
    its human. The loser of the race blocks, then re-reads and is judged
    against the state the winner actually wrote.

    ``actor`` says who is asking. Only the runtime, which establishes these
    states from real evidence, may name the lifecycle ones
    (:data:`CLIENT_TRANSITION_TARGETS` is the rest); an external client asking
    for one raises :class:`InvalidActorError` before the machine is consulted,
    because the edge being legal is beside the point.

    The task is resolved first on purpose: an id from another tenant has to keep
    answering 404, not turn into a 403 that would report on rows the caller
    cannot see.
    """
    task = await _load_task_for_update(
        session, task_id=task_id, tenant_id=tenant_id, user_id=user_id
    )
    if actor == EXTERNAL_ACTOR and target not in CLIENT_TRANSITION_TARGETS:
        raise InvalidActorError(
            f"state {target} is asserted by the agent runtime, not by a client; "
            f"a client may only request {', '.join(sorted(CLIENT_TRANSITION_TARGETS))}"
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