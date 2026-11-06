"""Planner API — two minimal verbs over the persisted plan history.

- ``POST /tasks/{task_id}/plan`` asks the fixed model for a proposal, keeps it
  only if it passes every deterministic gate, then advances the task to
  ``PLANNING`` through the Task Manager.
- ``GET /tasks/{task_id}/plan`` returns the latest accepted proposal (or one
  exact version via ``?version=`` for audit, including superseded rows).

Errors map to: 404 unknown task / 409 wrong state, rejected plan, or unresolved
runtime identities / 502 model unavailable. Replanning versioned, never silently
replaced.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kai.auth.deps import get_current_user
from kai.db.engine import get_session
from kai.db.models import User
from kai.planner.deps import get_planner_model
from kai.planner.model import PlannerModel
from kai.planner.models import TaskPlan
from kai.planner.schemas import PlanListResponse, PlanProposal, PlanResponse
from kai.planner.service import (
    ModelUnavailableError,
    PlannerService,
    PlanNotAllowedError,
    PlanNotFoundError,
    PlanRejectedError,
)
from kai.runtime.models import RuntimeExecution
from kai.tasks.service import TaskNotFoundError, get_task

router = APIRouter(prefix="/tasks", tags=["planner"])


def _to_response(plan: TaskPlan) -> PlanResponse:
    return PlanResponse(
        plan_id=str(plan.id),
        task_id=str(plan.task_id),
        tenant_id=str(plan.tenant_id),
        version=plan.version,
        status=plan.status,
        model_id=plan.model_id,
        created_at=plan.created_at,
        plan=PlanProposal.model_validate(plan.plan_json),
    )


async def _runtime_identities_missing(
    session: AsyncSession, *, task_id: UUID, tenant_id: UUID
) -> bool:
    """True only when this tenant's runtime execution exists with no identities.

    The lookup mirrors ``AgentRuntime._load_execution`` exactly: the same two
    predicates (``task_id`` and ``tenant_id``) and ``scalar_one_or_none``. A task
    submitted through the Agent Runtime owns one ``RuntimeExecution`` row; a task
    created directly through the task API owns none, so standalone planning is
    unaffected.

    The three persisted states this guard has to distinguish:

    * W1 -- ``scenario IS NULL`` and empty identities. Nothing has been resolved
      yet; the Runtime must remain able to run understanding and identity.
    * W2 -- a scenario is persisted but identities are still empty. The Runtime
      must remain able to resume at its identity stage.
    * W3 -- identities are already persisted and the task is not yet PLANNING.
      This must **not** be rejected: the identity set is durable, so the
      existing planning flow (with its own state and validation gates) runs.

    The check is presence-only. It never writes the execution row, transitions
    the task, or resolves/filters/promotes identities.

    Callers must establish that the requesting user may access the task first:
    this function answers only whether that authorized task's identity set is
    durable, and its answer must never be observable for a task the caller
    cannot access.
    """
    result = await session.execute(
        select(RuntimeExecution).where(
            RuntimeExecution.task_id == task_id,
            RuntimeExecution.tenant_id == tenant_id,
        )
    )
    row = result.scalar_one_or_none()
    return row is not None and not row.identities


def _refuse_unresolved_identities(task_id: UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": (
                "planning cannot proceed until the runtime's identity-resolution "
                "stage has produced and persisted the task's identity set"
            ),
            "current_task_id": str(task_id),
        },
    )


@router.post("/{task_id}/plan", response_model=PlanResponse, status_code=201)
async def plan_create(
    task_id: UUID,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    model: PlannerModel = Depends(get_planner_model),
) -> PlanResponse:
    """Generate + validate + persist a plan proposal for one task."""
    service = PlannerService(model)
    try:
        # Authorize first, inspect identity state second, plan third. The
        # canonical lookup scopes by (tenant, user, task) and raises the same
        # TaskNotFoundError for an unknown task and for one this caller may not
        # access, so the identity guard below can never turn an authorization
        # failure into a 409 that discloses the target's lifecycle state. This
        # is a separate scoped read from the one ``create_plan`` performs:
        # reusing the platform's access-control predicate is worth one indexed
        # lookup, and duplicating its predicates here would be worse.
        await get_task(
            session,
            task_id=task_id,
            tenant_id=current.tenant_id,
            user_id=current.id,
        )

        # Refuse before any plan is created, so no model call is made and no
        # TaskPlan row is written for an authorized runtime task whose
        # identities are not yet durable. See ``_runtime_identities_missing``
        # for W1/W2/W3.
        if await _runtime_identities_missing(
            session, task_id=task_id, tenant_id=current.tenant_id
        ):
            raise _refuse_unresolved_identities(task_id)

        plan = await service.create_plan(
            session,
            task_id=task_id,
            tenant_id=current.tenant_id,
            user_id=current.id,
        )
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except PlanNotAllowedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": str(exc), "current_task_id": str(task_id)},
        ) from exc
    except PlanRejectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "plan proposal rejected: " + str(exc),
                "current_task_id": str(task_id),
            },
        ) from exc
    except ModelUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="planner model unavailable: " + str(exc),
        ) from exc
    return _to_response(plan)


@router.get("/{task_id}/plan", response_model=PlanResponse)
async def plan_get(
    task_id: UUID,
    version: int | None = None,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    model: PlannerModel = Depends(get_planner_model),
) -> PlanResponse:
    """Latest accepted plan proposal (or one exact version for audit)."""
    service = PlannerService(model)
    try:
        plan = await service.get_plan(
            session,
            task_id=task_id,
            tenant_id=current.tenant_id,
            user_id=current.id,
            version=version,
        )
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except PlanNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return _to_response(plan)


@router.get("/{task_id}/plans", response_model=PlanListResponse)
async def plan_list(
    task_id: UUID,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    model: PlannerModel = Depends(get_planner_model),
) -> PlanListResponse:
    """Full version history (accepted + superseded), newest first."""
    service = PlannerService(model)
    try:
        plans = await service.list_plans(
            session,
            task_id=task_id,
            tenant_id=current.tenant_id,
            user_id=current.id,
        )
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return PlanListResponse(plans=[_to_response(p) for p in plans])