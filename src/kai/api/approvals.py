"""Approval Manager API — decisions only, no intent editing.

Every endpoint is authenticated and scoped by the bearer token's
``(tenant_id, user_id)``. There is no route that accepts a tenant_id, user_id,
tool arguments, or expiry from the client, and there is deliberately no
``POST /approvals/execute``: approve/reject only record the human decision (and
move the task through the Task Manager machine). The runtime/gateway is what
re-attempts execution later.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from kai.approvals.errors import (
    ApprovalNotFoundError,
    ApprovalSuggestionNotFoundError,
    ExpiredApprovalError,
    InvalidApprovalTransition,
    InvalidSuggestionTransition,
)
from kai.approvals.models import Approval, ApprovalRuleSuggestion, ApprovalStatus
from kai.approvals.schemas import (
    ApprovalDecisionRequest,
    ApprovalListResponse,
    ApprovalResponse,
    SuggestionDecisionRequest,
    SuggestionListResponse,
    SuggestionResponse,
)
from kai.approvals.service import ApprovalAction, ApprovalService
from kai.auth.deps import get_current_user
from kai.db.engine import get_session
from kai.db.models import User

router = APIRouter(prefix="/approvals", tags=["approvals"])

approval_service = ApprovalService()


def _approval_response(approval: Approval) -> ApprovalResponse:
    return ApprovalResponse.model_validate(
        {
            "id": approval.id,
            "tenant_id": approval.tenant_id,
            "user_id": approval.user_id,
            "task_id": approval.task_id,
            "tool_name": approval.tool_name,
            "risk_level": approval.risk_level,
            "status": approval.status,
            "recipient_class": approval.recipient_class,
            "intent_hash": approval.intent_hash,
            "request_summary": approval.request_summary,
            "created_at": approval.created_at,
            "expires_at": approval.expires_at,
            "decided_at": approval.decided_at,
            "decided_by": approval.decided_by,
            "decision_reason": approval.decision_reason,
            "consumed_at": approval.consumed_at,
        }
    )


def _suggestion_response(
    suggestion: ApprovalRuleSuggestion,
) -> SuggestionResponse:
    return SuggestionResponse.model_validate(
        {
            "id": suggestion.id,
            "tenant_id": suggestion.tenant_id,
            "user_id": suggestion.user_id,
            "tool_name": suggestion.tool_name,
            "risk_level": suggestion.risk_level,
            "recipient_class": suggestion.recipient_class,
            "allowed_tools": list(suggestion.allowed_tools),
            "allowed_recipients": list(suggestion.allowed_recipients),
            "risk_ceiling": suggestion.risk_ceiling,
            "expiration": suggestion.expiration,
            "status": suggestion.status,
            "created_at": suggestion.created_at,
            "rejected_at": suggestion.rejected_at,
            "reject_reason": suggestion.reject_reason,
        }
    )


@router.get("", response_model=ApprovalListResponse)
async def approval_list(
    status_filter: ApprovalStatus | None = Query(
        default=None, alias="status", description="only one approval status"
    ),
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ApprovalListResponse:
    approvals = await approval_service.list_approvals(
        session=session,
        tenant_id=current.tenant_id,
        user_id=current.id,
        status=status_filter.value if status_filter is not None else None,
    )
    return ApprovalListResponse(
        approvals=[_approval_response(item) for item in approvals]
    )


@router.get("/suggestions", response_model=SuggestionListResponse)
async def suggestion_list(
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SuggestionListResponse:
    suggestions = await approval_service.list_suggestions(
        session=session,
        tenant_id=current.tenant_id,
        user_id=current.id,
    )
    return SuggestionListResponse(
        suggestions=[_suggestion_response(item) for item in suggestions]
    )


@router.post("/suggestions/{suggestion_id}/reject", response_model=SuggestionResponse)
async def suggestion_reject(
    suggestion_id: UUID,
    body: SuggestionDecisionRequest,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SuggestionResponse:
    try:
        suggestion = await approval_service.reject_suggestion(
            session=session,
            suggestion_id=suggestion_id,
            tenant_id=current.tenant_id,
            user_id=current.id,
            reason=body.reason,
        )
    except ApprovalSuggestionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except InvalidSuggestionTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return _suggestion_response(suggestion)


@router.get("/{approval_id}", response_model=ApprovalResponse)
async def approval_get(
    approval_id: UUID,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ApprovalResponse:
    try:
        approval = await approval_service.get(
            session=session,
            approval_id=approval_id,
            tenant_id=current.tenant_id,
            user_id=current.id,
        )
    except ApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return _approval_response(approval)


@router.post("/{approval_id}/approve", response_model=ApprovalResponse)
async def approval_approve(
    approval_id: UUID,
    body: ApprovalDecisionRequest,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ApprovalResponse:
    return await _decide(approval_id, ApprovalAction.APPROVE, body, current, session)


@router.post("/{approval_id}/reject", response_model=ApprovalResponse)
async def approval_reject(
    approval_id: UUID,
    body: ApprovalDecisionRequest,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ApprovalResponse:
    return await _decide(approval_id, ApprovalAction.REJECT, body, current, session)


async def _decide(
    approval_id: UUID,
    action: ApprovalAction,
    body: ApprovalDecisionRequest,
    current: User,
    session: AsyncSession,
) -> ApprovalResponse:
    try:
        approval = await approval_service.decide(
            session=session,
            approval_id=approval_id,
            decision=action,
            tenant_id=current.tenant_id,
            user_id=current.id,
            reason=body.reason,
        )
    except ApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ExpiredApprovalError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except InvalidApprovalTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return _approval_response(approval)


__all__ = ["router"]