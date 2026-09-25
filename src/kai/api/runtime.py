"""Agent Runtime API — the slice's three minimal verbs.

- ``POST /runtime/tasks`` submits a goal + timezone, creates a ``CREATED`` task
  with the sealed slice contract, and persists its execution envelope.
- ``GET /runtime/tasks/{task_id}`` inspects the task's runtime progress.
- ``POST /runtime/tasks/{task_id}/trigger`` drives the task forward one call
  and reports where it stopped and why.

Errors map to: 404 unknown/foreign task, 409 not submitted through the runtime
or in a non-resumable state. Terminal tasks answer idempotently.
"""

from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from kai.auth.deps import get_current_user
from kai.db.engine import get_session
from kai.db.models import User
from kai.google.deps import get_google_http_client
from kai.runtime.deps import get_runtime_service
from kai.runtime.schemas import (
    GoalSubmission,
    RuntimeTaskResponse,
    SubmitResponse,
    TriggerResponse,
    UnderstandingOutput,
)
from kai.runtime.service import (
    AgentRuntime,
    RuntimeNotFoundError,
    RuntimeResumeError,
)
from kai.tasks.service import TaskNotFoundError, get_task

router = APIRouter(prefix="/runtime", tags=["agent-runtime"])


@router.post("/tasks", response_model=SubmitResponse, status_code=201)
async def runtime_submit(
    submission: GoalSubmission,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    runtime: AgentRuntime = Depends(get_runtime_service),
) -> SubmitResponse:
    """Create a runtime task with the fixed slice contract."""
    task = await runtime.submit(
        session, tenant_id=current.tenant_id, user_id=current.id,
        submission=submission,
    )
    return SubmitResponse(
        task_id=str(task.id), task_state=task.state, goal=task.goal
    )


@router.get("/tasks/{task_id}", response_model=RuntimeTaskResponse)
async def runtime_get(
    task_id: UUID,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    runtime: AgentRuntime = Depends(get_runtime_service),
) -> RuntimeTaskResponse:
    """Snapshot of the runtime execution envelope for one task."""
    try:
        task = await get_task(
            session,
            task_id=task_id,
            tenant_id=current.tenant_id,
            user_id=current.id,
        )
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    row = await runtime.execution_row(
        session, task_id=task.id, tenant_id=task.tenant_id
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="task was not submitted through the agent runtime",
        )

    approval_ids = await runtime.pending_approval_ids(session, task)
    if approval_ids:
        next_action = "APPROVE"
    elif task.state in ("COMPLETED", "FAILED", "REQUIRES_ATTENTION", "ESCALATE"):
        next_action = "DONE"
    else:
        next_action = "EXECUTE"
    return RuntimeTaskResponse(
        task_id=str(task.id),
        task_state=task.state,
        goal=task.goal,
        timezone=row.timezone,
        scenario=(
            UnderstandingOutput.model_validate(row.scenario)
            if row.scenario is not None
            else None
        ),
        identities=list(row.identities),
        evidence=list(row.evidence),
        cursor=row.cursor,
        steps_used=row.steps_used,
        next_action=next_action,
        approval_ids=approval_ids,
        last_error=task.last_error,
    )


@router.post("/tasks/{task_id}/trigger", response_model=TriggerResponse)
async def runtime_trigger(
    task_id: UUID,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    runtime: AgentRuntime = Depends(get_runtime_service),
    transport: httpx.AsyncClient = Depends(get_google_http_client),
) -> TriggerResponse:
    """Advance the task as far as it can go; report where it stopped."""
    try:
        outcome = await runtime.trigger(
            session,
            tenant_id=current.tenant_id,
            user_id=current.id,
            task_id=task_id,
            transport=transport,
        )
    except RuntimeNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except RuntimeResumeError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return TriggerResponse(
        task_id=str(task_id),
        task_state=outcome.task_state,
        outcome=outcome.outcome,
        message=outcome.message,
        approval_ids=outcome.approval_ids,
        steps_used=outcome.steps_used,
    )