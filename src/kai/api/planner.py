"""Planner API — two minimal verbs over the persisted plan history.

- ``POST /tasks/{task_id}/plan`` asks the fixed model for a proposal, keeps it
  only if it passes every deterministic gate, then advances the task to
  ``PLANNING`` through the Task Manager.
- ``GET /tasks/{task_id}/plan`` returns the latest accepted proposal (or one
  exact version via ``?version=`` for audit, including superseded rows).

Errors map to: 404 unknown task / 409 wrong state or rejected plan / 502 model
unavailable. Replanning versioned, never silently replaced.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
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
from kai.tasks.service import TaskNotFoundError

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