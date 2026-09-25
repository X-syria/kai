"""Task Manager API — minimal, no Planner, no LLM.

Only four verbs for now: create, get, transition, list (plus archive, which is
the only terminal-state data path). The caller supplies ``tenant_id`` and
``user_id`` explicitly and the service re-scopes every query with the
``WHERE tenant_id = ?`` predicate in :mod:`kai.tasks.service`, so a task id
from another tenant resolves to 404 and no row can be read or mutated through
a foreign tenant context. Transition refusal is a 409 Conflict, not a
silently-ignored no-op. A transition the caller is not entitled to ask for at
all - a runtime lifecycle state such as ``COMPLETED`` - is 403 Forbidden,
which is a different answer from "the machine would not allow that edge".
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from kai.auth.deps import get_current_user
from kai.db.engine import get_session
from kai.db.models import User
from kai.tasks.models import Task
from kai.tasks.schemas import (
    StateTransitionRequest,
    TaskContractInput,
    TaskListResponse,
    TaskResponse,
)
from kai.tasks.service import (
    CLIENT_TRANSITION_TARGETS,
    EXTERNAL_ACTOR,
    InvalidActorError,
    InvalidTransition,
    TaskNotFoundError,
    archive_task,
    create_task,
    get_task,
    list_tasks,
    transition_task,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _serialize(task: Task) -> dict:
    return {
        "id": str(task.id),
        "tenant_id": str(task.tenant_id),
        "user_id": str(task.user_id),
        "state": task.state,
        "contract": task.contract_dict(),
    }


def _to_response(task: Task) -> TaskResponse:
    return TaskResponse.model_validate(_serialize(task))


@router.post("", response_model=TaskResponse, status_code=201)
async def task_create(
    body: TaskContractInput,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> TaskResponse:
    """Create a task owned by the authenticated principal.

    ``tenant_id``/``user_id`` come from the token, never from the request body,
    so there is no field a caller could abuse to name another tenant's scope.
    """
    task = await create_task(
        session,
        tenant_id=current.tenant_id,
        user_id=current.id,
        contract=body,
    )
    return _to_response(task)


@router.get("", response_model=TaskListResponse)
async def task_list(
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskListResponse:
    tasks = await list_tasks(
        session, tenant_id=current.tenant_id, user_id=current.id
    )
    return TaskListResponse(tasks=[_to_response(t) for t in tasks])


@router.get("/{task_id}", response_model=TaskResponse)
async def task_get(
    task_id: UUID,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskResponse:
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
    return _to_response(task)


@router.post("/{task_id}/transition", response_model=TaskResponse)
async def task_transition(
    task_id: UUID,
    body: StateTransitionRequest,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskResponse:
    """Move the caller's own task, within the states a client may assert.

    A client may fail, escalate-for-attention, or retry a task it owns. It may
    not name a lifecycle state: those are the runtime's to assert from evidence
    it gathered, so asking for one is 403 Forbidden rather than a transition
    the machine happens to allow. Refusing a legal edge is 409 Conflict.
    """
    try:
        task = await transition_task(
            session,
            task_id=task_id,
            tenant_id=current.tenant_id,
            user_id=current.id,
            target=body.target_state,
            actor=EXTERNAL_ACTOR,
        )
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except InvalidActorError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": str(exc),
                "current_task_id": str(task_id),
                "client_transition_targets": sorted(CLIENT_TRANSITION_TARGETS),
            },
        ) from exc
    except InvalidTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": str(exc), "current_task_id": str(task_id)},
        ) from exc
    return _to_response(task)


@router.post("/{task_id}/archive", response_model=TaskResponse)
async def task_archive(
    task_id: UUID,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskResponse:
    try:
        task = await archive_task(
            session,
            task_id=task_id,
            tenant_id=current.tenant_id,
            user_id=current.id,
        )
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except InvalidTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": str(exc), "current_task_id": str(task_id)},
        ) from exc
    return _to_response(task)